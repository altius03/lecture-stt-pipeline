package shadow

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strconv"
	"time"
)

const (
	ScanRequestSchemaVersion   = "lecture-stt/shadow-scan-request@1"
	ScanResultSchemaVersion    = "lecture-stt/shadow-scan-result@1"
	maxLockstepLineBytes       = 64 * 1024
	defaultScanResponseTimeout = 15 * time.Second
	defaultProbeCloseTimeout   = 5 * time.Second
)

var errLockstepReadTimeout = errors.New("lockstep read timed out")
var errLockstepCloseTimeout = errors.New("lockstep close timed out")

type scanRequest struct {
	SchemaVersion string `json:"schema_version"`
	ScanIndex     int    `json:"scan_index"`
}

type scanResult struct {
	ScanIndex           int      `json:"scan_index"`
	SchemaVersion       string   `json:"schema_version"`
	StableRelativePaths []string `json:"stable_relative_paths"`
}

type lockstepProbe struct {
	command         *exec.Cmd
	closeTimeout    time.Duration
	input           io.WriteCloser
	output          *bufio.Reader
	outputCloser    io.Closer
	responseTimeout time.Duration
	stderr          *boundedBuffer
	waited          bool
}

func startLockstepProbe(options Options, scanCount int) (*lockstepProbe, error) {
	args := []string{
		"lockstep-scan",
		"--config",
		options.ConfigPath,
		"--scan-count",
		strconv.Itoa(scanCount),
	}
	if options.WatchFolderOverride != "" {
		args = append(args, "--watch-folder", options.WatchFolderOverride)
	}
	command := pythonCommand(options, "lecture_stt.stt.shadow_probe", args...)
	stderr := &boundedBuffer{limit: maxPlanBytes}
	command.Stderr = stderr
	input, err := command.StdinPipe()
	if err != nil {
		return nil, err
	}
	output, err := command.StdoutPipe()
	if err != nil {
		_ = input.Close()
		return nil, err
	}
	if err := command.Start(); err != nil {
		_ = input.Close()
		return nil, err
	}
	return &lockstepProbe{
		command:         command,
		closeTimeout:    defaultProbeCloseTimeout,
		input:           input,
		output:          bufio.NewReader(output),
		outputCloser:    output,
		responseTimeout: defaultScanResponseTimeout,
		stderr:          stderr,
	}, nil
}

func (probe *lockstepProbe) Scan(scanIndex int) ([]string, error) {
	return probe.ScanContext(context.Background(), scanIndex)
}

func (probe *lockstepProbe) ScanContext(
	ctx context.Context,
	scanIndex int,
) ([]string, error) {
	request, err := json.Marshal(scanRequest{
		ScanIndex:     scanIndex,
		SchemaVersion: ScanRequestSchemaVersion,
	})
	if err != nil {
		return nil, err
	}
	request = append(request, '\n')
	if _, err := probe.input.Write(request); err != nil {
		return nil, errors.New("write lockstep scan request")
	}
	line, err := readBoundedLineWithContext(
		ctx,
		probe.output,
		maxLockstepLineBytes,
		probe.responseTimeout,
	)
	if err != nil {
		if errors.Is(err, context.Canceled) {
			probe.Abort()
			return nil, context.Canceled
		}
		if errors.Is(err, errLockstepReadTimeout) {
			probe.Abort()
			return nil, errors.New("Python lockstep scan timed out waiting for a response")
		}
		return nil, errors.New("read lockstep scan result")
	}
	result, err := decodeScanResult(line, scanIndex)
	if err != nil {
		return nil, err
	}
	return result.StableRelativePaths, nil
}

func decodeScanResult(value []byte, expectedScanIndex int) (scanResult, error) {
	if len(value) == 0 || len(value) > maxLockstepLineBytes {
		return scanResult{}, errors.New("Python lockstep scan result size is invalid")
	}
	if err := rejectDuplicateJSONKeys(value); err != nil {
		return scanResult{}, err
	}
	var result scanResult
	if err := strictDecode(value, &result); err != nil {
		return scanResult{}, err
	}
	if result.SchemaVersion != ScanResultSchemaVersion {
		return scanResult{}, errors.New("unsupported Python lockstep scan result schema")
	}
	if result.ScanIndex != expectedScanIndex {
		return scanResult{}, errors.New("Python lockstep scan index mismatch")
	}
	if result.StableRelativePaths == nil {
		return scanResult{}, errors.New("Python lockstep scan paths must be an array")
	}
	seen := make(map[string]struct{}, len(result.StableRelativePaths))
	for _, name := range result.StableRelativePaths {
		if err := validateDirectName(name); err != nil {
			return scanResult{}, errors.New("Python lockstep scan emitted an invalid relative path")
		}
		if _, ok := seen[name]; ok {
			return scanResult{}, errors.New("Python lockstep scan emitted a duplicate relative path")
		}
		seen[name] = struct{}{}
	}
	return result, nil
}

func (probe *lockstepProbe) Close() error {
	return probe.CloseContext(context.Background())
}

func (probe *lockstepProbe) CloseContext(ctx context.Context) error {
	if probe.waited {
		return nil
	}
	_ = probe.input.Close()

	type closeResult struct {
		tail    []byte
		tailErr error
		waitErr error
	}
	closeDone := make(chan closeResult, 1)
	go func() {
		tail, tailErr := readBoundedTail(probe.output, maxPlanBytes)
		if tailErr != nil {
			if probe.outputCloser != nil {
				_ = probe.outputCloser.Close()
			}
			terminateSubprocess(probe.command)
		}
		closeDone <- closeResult{
			tail:    tail,
			tailErr: tailErr,
			waitErr: probe.command.Wait(),
		}
	}()

	timeout := probe.closeTimeout
	if timeout <= 0 {
		timeout = defaultProbeCloseTimeout
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()

	timedOut := false
	for {
		select {
		case result := <-closeDone:
			probe.waited = true
			if timedOut {
				return errLockstepCloseTimeout
			}
			if result.tailErr != nil {
				return result.tailErr
			}
			if len(bytes.TrimSpace(result.tail)) != 0 {
				return errors.New("Python lockstep scan emitted unexpected trailing output")
			}
			if result.waitErr != nil {
				return errors.New("Python lockstep scan process failed")
			}
			return nil
		case <-ctx.Done():
			terminateSubprocess(probe.command)
			if probe.outputCloser != nil {
				_ = probe.outputCloser.Close()
			}
			<-closeDone
			probe.waited = true
			return context.Canceled
		case <-timer.C:
			if timedOut {
				probe.waited = true
				return errLockstepCloseTimeout
			}
			timedOut = true
			if probe.command.Process != nil {
				terminateSubprocess(probe.command)
			}
			if probe.outputCloser != nil {
				_ = probe.outputCloser.Close()
			}
			timer.Reset(defaultProbeCloseTimeout)
		}
	}
}

func (probe *lockstepProbe) Abort() {
	if probe.waited {
		return
	}
	_ = probe.input.Close()
	terminateSubprocess(probe.command)
	_ = probe.command.Wait()
	probe.waited = true
}

func readBoundedLine(reader *bufio.Reader, limit int) ([]byte, error) {
	line := make([]byte, 0)
	for {
		fragment, continued, err := reader.ReadLine()
		if err != nil {
			return nil, err
		}
		if len(line)+len(fragment) > limit {
			return nil, fmt.Errorf("lockstep line exceeds %d bytes", limit)
		}
		line = append(line, fragment...)
		if !continued {
			return line, nil
		}
	}
}

func readBoundedLineWithTimeout(reader *bufio.Reader, limit int, timeout time.Duration) ([]byte, error) {
	return readBoundedLineWithContext(
		context.Background(),
		reader,
		limit,
		timeout,
	)
}

func readBoundedLineWithContext(
	ctx context.Context,
	reader *bufio.Reader,
	limit int,
	timeout time.Duration,
) ([]byte, error) {
	if timeout <= 0 {
		select {
		case <-ctx.Done():
			return nil, context.Canceled
		default:
			return readBoundedLine(reader, limit)
		}
	}
	type result struct {
		line []byte
		err  error
	}
	done := make(chan result, 1)
	go func() {
		line, err := readBoundedLine(reader, limit)
		done <- result{line: line, err: err}
	}()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case outcome := <-done:
		return outcome.line, outcome.err
	case <-ctx.Done():
		return nil, context.Canceled
	case <-timer.C:
		return nil, errLockstepReadTimeout
	}
}

func readBoundedTail(reader io.Reader, limit int) ([]byte, error) {
	result := make([]byte, 0)
	buffer := make([]byte, 16*1024)
	for {
		count, err := reader.Read(buffer)
		if count > 0 {
			if len(result)+count > limit {
				return nil, fmt.Errorf("lockstep trailing output exceeds %d bytes", limit)
			}
			result = append(result, buffer[:count]...)
		}
		if errors.Is(err, io.EOF) {
			return result, nil
		}
		if err != nil {
			return nil, err
		}
	}
}
