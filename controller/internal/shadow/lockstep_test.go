package shadow

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestDecodeScanResultRejectsClosedSchemaAndScanIndexViolations(t *testing.T) {
	invalidPayloads := []struct {
		name      string
		payload   string
		targetIdx int
	}{
		{
			name:      "duplicate_json_key",
			payload:   `{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":["stable.m4a"],"schema_version":"dup"}`,
			targetIdx: 1,
		},
		{
			name:      "unknown_json_field",
			payload:   `{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":[],"unexpected":true}`,
			targetIdx: 1,
		},
		{
			name:      "null_relative_path_list",
			payload:   `{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":null}`,
			targetIdx: 1,
		},
		{
			name:      "duplicate_relative_path",
			payload:   `{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":["a.m4a","a.m4a"]}`,
			targetIdx: 1,
		},
		{
			name:      "scan_index_mismatch",
			payload:   `{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":[]}`,
			targetIdx: 2,
		},
	}

	for _, tc := range invalidPayloads {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			_, err := decodeScanResult([]byte(tc.payload), tc.targetIdx)
			if err == nil {
				t.Fatalf("expected scan result rejection for %s", tc.name)
			}
		})
	}
}

func TestDecodeScanResultAcceptsClosedSchema(t *testing.T) {
	result, err := decodeScanResult(
		[]byte(`{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":7,"stable_relative_paths":["stable.m4a","temp.m4a"]}`),
		7,
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.ScanIndex != 7 || len(result.StableRelativePaths) != 2 {
		t.Fatalf("unexpected decoded scan result: %#v", result)
	}
	if result.SchemaVersion != ScanResultSchemaVersion {
		t.Fatalf("expected schema version %q, got %q", ScanResultSchemaVersion, result.SchemaVersion)
	}
}

func TestReadBoundedLineRejectsOversizedAndEOFInput(t *testing.T) {
	t.Run("oversized", func(t *testing.T) {
		oversized := bytes.Repeat([]byte("x"), maxLockstepLineBytes+1)
		oversized = append(oversized, '\n')
		_, err := readBoundedLine(bufio.NewReader(bytes.NewReader(oversized)), maxLockstepLineBytes)
		if err == nil {
			t.Fatal("expected oversized lockstep line rejection")
		}
	})

	t.Run("eof", func(t *testing.T) {
		_, err := readBoundedLine(bufio.NewReader(strings.NewReader("")), 32)
		if err == nil {
			t.Fatal("expected EOF for empty reader")
		}
		if !errors.Is(err, io.EOF) {
			t.Fatalf("expected EOF, got %v", err)
		}
	})
}

func TestReadBoundedLineWithTimeoutRejectsStalledReader(t *testing.T) {
	reader, writer := io.Pipe()
	defer writer.Close()

	_, err := readBoundedLineWithTimeout(bufio.NewReader(reader), 32, 10*time.Millisecond)
	if !errors.Is(err, errLockstepReadTimeout) {
		t.Fatalf("expected lockstep read timeout, got %v", err)
	}
}

func TestLockstepProbeCloseKillsStalledChild(t *testing.T) {
	if os.Getenv("LECTURE_STT_LOCKSTEP_CLOSE_STALL_HELPER") == "1" {
		time.Sleep(time.Hour)
		return
	}

	command := exec.Command(os.Args[0], "-test.run=^TestLockstepProbeCloseKillsStalledChild$")
	command.Env = append(os.Environ(), "LECTURE_STT_LOCKSTEP_CLOSE_STALL_HELPER=1")
	input, err := command.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	output, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}

	probe := &lockstepProbe{
		closeTimeout: 25 * time.Millisecond,
		command:      command,
		input:        input,
		output:       bufio.NewReader(output),
		outputCloser: output,
		stderr:       &boundedBuffer{limit: 1024},
	}
	started := time.Now()
	err = probe.Close()
	if !errors.Is(err, errLockstepCloseTimeout) {
		t.Fatalf("expected close timeout, got %v", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("stalled child cleanup exceeded bound: %s", elapsed)
	}
	if !probe.waited || command.ProcessState == nil {
		t.Fatal("stalled child was not reaped")
	}
}

func TestLockstepProbeScanContextCancellationReapsChild(t *testing.T) {
	if os.Getenv("LECTURE_STT_LOCKSTEP_CANCEL_HELPER") == "1" {
		time.Sleep(time.Hour)
		return
	}

	command := exec.Command(os.Args[0], "-test.run=^TestLockstepProbeScanContextCancellationReapsChild$")
	command.Env = append(os.Environ(), "LECTURE_STT_LOCKSTEP_CANCEL_HELPER=1")
	configureSubprocess(command)
	input, err := command.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	output, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}

	probe := &lockstepProbe{
		command:         command,
		input:           input,
		output:          bufio.NewReader(output),
		outputCloser:    output,
		responseTimeout: time.Hour,
		stderr:          &boundedBuffer{limit: 1024},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Millisecond)
	defer cancel()
	started := time.Now()
	_, err = probe.ScanContext(ctx, 1)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context cancellation, got %v", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("canceled lockstep cleanup exceeded bound: %s", elapsed)
	}
	if !probe.waited || command.ProcessState == nil {
		t.Fatal("canceled lockstep child was not reaped")
	}
}
