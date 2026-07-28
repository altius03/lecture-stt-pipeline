package shadow

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	ConfigSchemaVersion = "lecture-stt/shadow-config@1"
	ScanSchemaVersion   = "lecture-stt/shadow-scan@1"
	ReportSchemaVersion = "lecture-stt/controller-shadow-report@4"
	maxProbeBytes       = 4 * 1024 * 1024
	maxScanCount        = 1000
	maxObservedPaths    = 4096
	maxPlanChecks       = 4096
)

type Options struct {
	PythonBin           string
	RepoRoot            string
	ConfigPath          string
	WatchFolderOverride string
	KillSwitchPath      string
	PythonStatScan      bool
	ScanHelperAttempts  int
	ScanTimeout         time.Duration
	ScanCount           int
	SleepSec            *float64
}

type ShadowConfig struct {
	SchemaVersion      string `json:"schema_version"`
	PollingIntervalSec int    `json:"polling_interval_sec"`
	StableForSec       int    `json:"stable_for_sec"`
	WatchFolder        string `json:"watch_folder"`
}

type probeScan struct {
	ScanIndex           int      `json:"scan_index"`
	StableRelativePaths []string `json:"stable_relative_paths"`
}

type probeScanReport struct {
	SchemaVersion      string      `json:"schema_version"`
	PollingIntervalSec int         `json:"polling_interval_sec"`
	ScanCount          int         `json:"scan_count"`
	Scans              []probeScan `json:"scans"`
	SleepSec           float64     `json:"sleep_sec"`
	StableForSec       int         `json:"stable_for_sec"`
}

type ScanComparison struct {
	GoStableRelativePaths     []string `json:"go_stable_relative_paths"`
	Match                     bool     `json:"match"`
	PythonStableRelativePaths []string `json:"python_stable_relative_paths"`
	ScanIndex                 int      `json:"scan_index"`
}

type PlanCheck struct {
	ErrorKind    string `json:"error_kind,omitempty"`
	PlanSHA256   string `json:"plan_sha256,omitempty"`
	RelativePath string `json:"relative_path"`
	ScanIndex    int    `json:"scan_index"`
	Status       string `json:"status"`
}

type Report struct {
	CompletedAt          string           `json:"completed_at"`
	ErrorKind            string           `json:"error_kind,omitempty"`
	KillSwitchConfigured bool             `json:"kill_switch_configured"`
	Mode                 string           `json:"mode"`
	OK                   bool             `json:"ok"`
	PlanChecks           []PlanCheck      `json:"plan_checks"`
	PollingMatch         bool             `json:"polling_match"`
	RunID                string           `json:"run_id"`
	ScanComparisons      []ScanComparison `json:"scan_comparisons"`
	ScanCount            int              `json:"scan_count"`
	SchemaVersion        string           `json:"schema_version"`
	StartedAt            string           `json:"started_at"`
	VerifiedCount        int              `json:"verified_count"`
}

type boundedBuffer struct {
	buffer bytes.Buffer
	limit  int
}

func (b *boundedBuffer) Write(value []byte) (int, error) {
	remaining := b.limit - b.buffer.Len()
	if remaining <= 0 {
		return 0, errors.New("subprocess output exceeded the closed size limit")
	}
	if len(value) > remaining {
		_, _ = b.buffer.Write(value[:remaining])
		return remaining, errors.New("subprocess output exceeded the closed size limit")
	}
	return b.buffer.Write(value)
}

func (b *boundedBuffer) Bytes() []byte {
	return b.buffer.Bytes()
}

func Run(options Options) (Report, error) {
	return RunContext(context.Background(), options)
}

func RunContext(ctx context.Context, options Options) (Report, error) {
	return runWithScanHookContext(ctx, options, nil)
}

func NewFailureReport(kind string) Report {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	runID, err := newRunID()
	if err != nil {
		kind = "run_metadata_failed"
	}
	return Report{
		CompletedAt:     now,
		ErrorKind:       kind,
		Mode:            "read_only",
		PlanChecks:      []PlanCheck{},
		RunID:           runID,
		ScanComparisons: []ScanComparison{},
		SchemaVersion:   ReportSchemaVersion,
		StartedAt:       now,
	}
}

type beforeScanHook func(scanIndex int, watchFolder string) error

func runWithScanHook(options Options, hook beforeScanHook) (Report, error) {
	return runWithScanHookContext(context.Background(), options, hook)
}

func runWithScanHookContext(
	ctx context.Context,
	options Options,
	hook beforeScanHook,
) (report Report, runErr error) {
	startedAt := time.Now().UTC()
	report = Report{
		SchemaVersion:   ReportSchemaVersion,
		Mode:            "read_only",
		PlanChecks:      []PlanCheck{},
		ScanComparisons: []ScanComparison{},
		StartedAt:       startedAt.Format(time.RFC3339Nano),
	}
	defer func() {
		completedAt := time.Now().UTC()
		if completedAt.Before(startedAt) {
			completedAt = startedAt
		}
		report.CompletedAt = completedAt.Format(time.RFC3339Nano)
	}()
	runID, err := newRunID()
	if err != nil {
		report.ErrorKind = "run_metadata_failed"
		return report, errors.New("could not create shadow run metadata")
	}
	report.RunID = runID
	normalized, err := normalizeOptions(options)
	if err != nil {
		report.ErrorKind = "invalid_options"
		return report, err
	}
	report.KillSwitchConfigured = normalized.KillSwitchPath != ""
	if err := checkRunBoundary(ctx, normalized.KillSwitchPath); err != nil {
		report.ErrorKind = boundaryErrorKind(err)
		return report, err
	}

	configBytes, err := runPythonContext(
		ctx,
		normalized,
		maxPlanBytes,
		"lecture_stt.stt.shadow_probe",
		configArguments(normalized)...,
	)
	if err != nil {
		if errors.Is(err, context.Canceled) {
			report.ErrorKind = "interrupted"
			return report, err
		}
		report.ErrorKind = "config_probe_failed"
		return report, err
	}
	config, err := decodeShadowConfig(configBytes)
	if err != nil {
		report.ErrorKind = "invalid_config_probe"
		return report, err
	}

	scanCount, sleepSec, err := resolveScanSchedule(normalized, config)
	if err != nil {
		report.ErrorKind = "invalid_schedule"
		return report, err
	}
	report.ScanCount = scanCount

	pythonProbe, err := startLockstepProbe(normalized, scanCount)
	if err != nil {
		report.ErrorKind = "scan_probe_failed"
		return report, errors.New("could not start the Python scan probe")
	}
	probeClosed := false
	defer func() {
		if !probeClosed {
			pythonProbe.Abort()
		}
	}()

	tracker := NewTracker(time.Duration(config.StableForSec) * time.Second)
	report.PollingMatch = true
	observedPaths := 0
	for index := 0; index < scanCount; index++ {
		if index > 0 && sleepSec > 0 {
			if err := waitContext(ctx, time.Duration(sleepSec*float64(time.Second))); err != nil {
				report.ErrorKind = "interrupted"
				return report, err
			}
		}
		if err := checkRunBoundary(ctx, normalized.KillSwitchPath); err != nil {
			report.ErrorKind = boundaryErrorKind(err)
			return report, err
		}
		scanIndex := index + 1
		if hook != nil {
			if err := hook(scanIndex, config.WatchFolder); err != nil {
				report.ErrorKind = "scan_hook_failed"
				return report, errors.New("isolated scan hook failed")
			}
		}
		entries, scanErr := scanWatchFolderContext(ctx, normalized, config.WatchFolder)
		if scanErr != nil {
			if errors.Is(scanErr, context.Canceled) {
				report.ErrorKind = "interrupted"
				return report, scanErr
			}
			report.ErrorKind = "go_scan_failed"
			return report, errors.New("Go shadow could not scan the watch folder")
		}
		goPaths := normalizedNames(tracker.Observe(entries, time.Now()))
		pythonPaths, scanErr := pythonProbe.ScanContext(ctx, scanIndex)
		if scanErr != nil {
			if errors.Is(scanErr, context.Canceled) {
				report.ErrorKind = "interrupted"
				return report, scanErr
			}
			report.ErrorKind = "scan_probe_failed"
			return report, errors.New("Python scan probe rejected the lockstep scan")
		}
		pythonPaths = normalizedNames(pythonPaths)
		observedPaths += len(goPaths) + len(pythonPaths)
		if observedPaths > maxObservedPaths {
			report.ErrorKind = "shadow_limit_exceeded"
			return report, errors.New("shadow report path observation limit exceeded")
		}
		match := equalStrings(goPaths, pythonPaths)
		if !match {
			report.PollingMatch = false
		}
		report.ScanComparisons = append(report.ScanComparisons, ScanComparison{
			GoStableRelativePaths:     goPaths,
			Match:                     match,
			PythonStableRelativePaths: pythonPaths,
			ScanIndex:                 scanIndex,
		})
		for _, name := range intersectStrings(goPaths, pythonPaths) {
			if err := checkRunBoundary(ctx, normalized.KillSwitchPath); err != nil {
				report.ErrorKind = boundaryErrorKind(err)
				return report, err
			}
			if len(report.PlanChecks) >= maxPlanChecks {
				report.ErrorKind = "shadow_limit_exceeded"
				return report, errors.New("shadow plan-check limit exceeded")
			}
			check := verifyPythonPlanContext(ctx, normalized, config.WatchFolder, name)
			if err := ctx.Err(); err != nil {
				report.ErrorKind = "interrupted"
				return report, context.Canceled
			}
			check.ScanIndex = scanIndex
			report.PlanChecks = append(report.PlanChecks, check)
			if check.Status == "verified" {
				report.VerifiedCount++
			}
		}
	}
	if err := checkRunBoundary(ctx, normalized.KillSwitchPath); err != nil {
		report.ErrorKind = boundaryErrorKind(err)
		return report, err
	}
	if err := pythonProbe.CloseContext(ctx); err != nil {
		if errors.Is(err, context.Canceled) {
			report.ErrorKind = "interrupted"
			return report, err
		}
		report.ErrorKind = "scan_probe_failed"
		return report, errors.New("Python scan probe did not close cleanly")
	}
	probeClosed = true
	report.OK = report.PollingMatch && report.VerifiedCount == len(report.PlanChecks)
	if !report.OK && report.ErrorKind == "" {
		report.ErrorKind = "shadow_mismatch"
	}
	return report, nil
}

func newRunID() (string, error) {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(value[:]), nil
}

func normalizeOptions(options Options) (Options, error) {
	if strings.TrimSpace(options.PythonBin) == "" {
		return Options{}, errors.New("python binary must be non-empty")
	}
	if strings.TrimSpace(options.RepoRoot) == "" || strings.TrimSpace(options.ConfigPath) == "" {
		return Options{}, errors.New("repo root and config path are required")
	}
	repoRoot, err := filepath.Abs(options.RepoRoot)
	if err != nil {
		return Options{}, errors.New("invalid repo root")
	}
	configPath, err := filepath.Abs(options.ConfigPath)
	if err != nil {
		return Options{}, errors.New("invalid config path")
	}
	sourceRoot := filepath.Join(repoRoot, "src", "lecture_stt")
	info, err := os.Stat(sourceRoot)
	if err != nil || !info.IsDir() {
		return Options{}, errors.New("repo root does not contain src/lecture_stt")
	}
	options.RepoRoot = repoRoot
	options.ConfigPath = configPath
	if options.WatchFolderOverride != "" {
		override, err := filepath.Abs(options.WatchFolderOverride)
		if err != nil {
			return Options{}, errors.New("invalid watch-folder override")
		}
		options.WatchFolderOverride = override
	}
	if options.KillSwitchPath != "" {
		killSwitchPath, err := filepath.Abs(options.KillSwitchPath)
		if err != nil {
			return Options{}, errors.New("invalid kill-switch path")
		}
		options.KillSwitchPath = killSwitchPath
	}
	if options.PythonStatScan {
		if options.ScanTimeout == 0 {
			options.ScanTimeout = 5 * time.Second
		}
		if options.ScanTimeout < 50*time.Millisecond || options.ScanTimeout > 30*time.Second {
			return Options{}, errors.New("scan helper timeout must be between 50ms and 30s")
		}
		if options.ScanHelperAttempts == 0 {
			options.ScanHelperAttempts = 3
		}
		if options.ScanHelperAttempts < 1 || options.ScanHelperAttempts > 3 {
			return Options{}, errors.New("scan helper attempts must be between 1 and 3")
		}
	} else if options.ScanTimeout != 0 {
		return Options{}, errors.New("scan helper timeout requires Python stat scan")
	} else if options.ScanHelperAttempts != 0 {
		return Options{}, errors.New("scan helper attempts require Python stat scan")
	}
	if options.ScanCount < 0 || options.ScanCount > maxScanCount {
		return Options{}, fmt.Errorf("scan-count must be between 0 and %d", maxScanCount)
	}
	if options.SleepSec != nil &&
		(math.IsNaN(*options.SleepSec) || math.IsInf(*options.SleepSec, 0) ||
			*options.SleepSec < 0 || *options.SleepSec > 3600) {
		return Options{}, errors.New("sleep-sec must be between 0 and 3600")
	}
	return options, nil
}

func decodeShadowConfig(value []byte) (ShadowConfig, error) {
	if err := rejectDuplicateJSONKeys(value); err != nil {
		return ShadowConfig{}, err
	}
	var config ShadowConfig
	if err := strictDecode(value, &config); err != nil {
		return ShadowConfig{}, err
	}
	if config.SchemaVersion != ConfigSchemaVersion {
		return ShadowConfig{}, errors.New("unsupported Python shadow config schema")
	}
	if config.StableForSec < 0 || config.PollingIntervalSec <= 0 {
		return ShadowConfig{}, errors.New("invalid Python shadow timing")
	}
	if !filepath.IsAbs(config.WatchFolder) {
		return ShadowConfig{}, errors.New("Python shadow watch folder must be absolute")
	}
	info, err := os.Stat(config.WatchFolder)
	if err != nil || !info.IsDir() {
		return ShadowConfig{}, errors.New("Python shadow watch folder is unavailable")
	}
	return config, nil
}

func resolveScanSchedule(options Options, config ShadowConfig) (int, float64, error) {
	sleepSec := float64(config.PollingIntervalSec)
	if options.SleepSec != nil {
		sleepSec = *options.SleepSec
	}
	if math.IsNaN(sleepSec) || math.IsInf(sleepSec, 0) || sleepSec < 0 || sleepSec > 3600 {
		return 0, 0, errors.New("resolved sleep-sec must be between 0 and 3600")
	}
	scanCount := options.ScanCount
	if scanCount == 0 {
		if sleepSec <= 0 {
			scanCount = 2
		} else {
			scanCount = int(math.Ceil(float64(config.StableForSec)/sleepSec)) + 2
		}
	}
	if scanCount <= 0 || scanCount > maxScanCount {
		return 0, 0, fmt.Errorf("resolved scan-count must be between 1 and %d", maxScanCount)
	}
	return scanCount, sleepSec, nil
}

func decodeProbeScanReport(
	value []byte,
	config ShadowConfig,
	scanCount int,
	sleepSec float64,
) (probeScanReport, error) {
	if len(value) == 0 || len(value) > maxProbeBytes {
		return probeScanReport{}, errors.New("Python scan report size is invalid")
	}
	if err := rejectDuplicateJSONKeys(value); err != nil {
		return probeScanReport{}, err
	}
	var report probeScanReport
	if err := strictDecode(value, &report); err != nil {
		return probeScanReport{}, err
	}
	if report.SchemaVersion != ScanSchemaVersion ||
		report.StableForSec != config.StableForSec ||
		report.PollingIntervalSec != config.PollingIntervalSec ||
		report.ScanCount != scanCount ||
		report.SleepSec != sleepSec ||
		len(report.Scans) != scanCount {
		return probeScanReport{}, errors.New("Python scan report contract mismatch")
	}
	for index, scan := range report.Scans {
		if scan.ScanIndex != index+1 {
			return probeScanReport{}, errors.New("Python scan indexes are not contiguous")
		}
		seen := make(map[string]struct{}, len(scan.StableRelativePaths))
		for _, name := range scan.StableRelativePaths {
			if err := validateDirectName(name); err != nil {
				return probeScanReport{}, errors.New("Python scan emitted an invalid relative path")
			}
			if _, ok := seen[name]; ok {
				return probeScanReport{}, errors.New("Python scan emitted a duplicate relative path")
			}
			seen[name] = struct{}{}
		}
	}
	return report, nil
}

func strictDecode(value []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(value))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	return requireJSONEOF(decoder)
}

func verifyPythonPlan(options Options, watchFolder, relativePath string) PlanCheck {
	return verifyPythonPlanContext(context.Background(), options, watchFolder, relativePath)
}

func verifyPythonPlanContext(
	ctx context.Context,
	options Options,
	watchFolder,
	relativePath string,
) PlanCheck {
	check, _ := buildVerifiedPythonPlanContext(
		ctx,
		options,
		watchFolder,
		relativePath,
	)
	return check
}

func buildVerifiedPythonPlanContext(
	ctx context.Context,
	options Options,
	watchFolder,
	relativePath string,
) (PlanCheck, []byte) {
	check := PlanCheck{RelativePath: relativePath, Status: "rejected"}
	args := []string{"--config", options.ConfigPath}
	if options.WatchFolderOverride != "" {
		args = append(args, "--watch-folder", options.WatchFolderOverride)
	}
	args = append(args, "--plan-single-job", relativePath)
	value, err := runPythonContext(
		ctx,
		options,
		maxPlanBytes,
		"lecture_stt.stt.main",
		args...,
	)
	if err != nil {
		check.ErrorKind = "python_plan_rejected"
		return check, nil
	}
	var plan Plan
	if options.PythonStatScan {
		plan, err = DecodeAndVerifyPlanContract(value)
	} else {
		plan, err = DecodeAndVerifyPlan(value, watchFolder)
	}
	if err != nil {
		var contractErr *ContractError
		if errors.As(err, &contractErr) {
			check.ErrorKind = contractErr.Kind
		} else {
			check.ErrorKind = "plan_verification_failed"
		}
		return check, nil
	}
	if plan.Source.RelativePath != relativePath {
		check.ErrorKind = "relative_path_mismatch"
		return check, nil
	}
	check.Status = "verified"
	check.PlanSHA256 = plan.PlanSHA256
	return check, value
}

func configArguments(options Options) []string {
	args := []string{"config", "--config", options.ConfigPath}
	if options.WatchFolderOverride != "" {
		args = append(args, "--watch-folder", options.WatchFolderOverride)
	}
	return args
}

func scanArguments(options Options, scanCount int, sleepSec float64) []string {
	args := []string{
		"scan",
		"--config",
		options.ConfigPath,
		"--scan-count",
		strconv.Itoa(scanCount),
		"--sleep-sec",
		strconv.FormatFloat(sleepSec, 'g', -1, 64),
	}
	if options.WatchFolderOverride != "" {
		args = append(args, "--watch-folder", options.WatchFolderOverride)
	}
	return args
}

func runPython(options Options, outputLimit int, module string, args ...string) ([]byte, error) {
	return runPythonContext(context.Background(), options, outputLimit, module, args...)
}

func runPythonContext(
	ctx context.Context,
	options Options,
	outputLimit int,
	module string,
	args ...string,
) ([]byte, error) {
	stdout := &boundedBuffer{limit: outputLimit}
	stderr := &boundedBuffer{limit: maxPlanBytes}
	command := pythonCommand(options, module, args...)
	command.Stdout = stdout
	command.Stderr = stderr
	if err := command.Start(); err != nil {
		return nil, errors.New("Python read-only command rejected")
	}
	waitDone := make(chan error, 1)
	go func() {
		waitDone <- command.Wait()
	}()
	select {
	case err := <-waitDone:
		if err != nil {
			return nil, errors.New("Python read-only command rejected")
		}
	case <-ctx.Done():
		terminateSubprocess(command)
		<-waitDone
		return nil, context.Canceled
	}
	if err := ctx.Err(); err != nil {
		return nil, context.Canceled
	}
	return stdout.Bytes(), nil
}

func pythonCommand(options Options, module string, args ...string) *exec.Cmd {
	commandArgs := append([]string{"-m", module}, args...)
	command := exec.Command(options.PythonBin, commandArgs...)
	command.Dir = options.RepoRoot
	command.Env = environmentWithPythonPath(filepath.Join(options.RepoRoot, "src"))
	configureSubprocess(command)
	return command
}

var errKillSwitchActive = errors.New("controller kill switch is active")
var errKillSwitchInvalid = errors.New("controller kill switch path is invalid")

func checkRunBoundary(ctx context.Context, killSwitchPath string) error {
	if err := ctx.Err(); err != nil {
		return context.Canceled
	}
	if killSwitchPath == "" {
		return nil
	}
	info, err := os.Lstat(killSwitchPath)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return errKillSwitchInvalid
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
		return errKillSwitchInvalid
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Nlink != 1 {
		return errKillSwitchInvalid
	}
	return errKillSwitchActive
}

func boundaryErrorKind(err error) string {
	switch {
	case errors.Is(err, context.Canceled):
		return "interrupted"
	case errors.Is(err, errKillSwitchActive):
		return "kill_switch_active"
	default:
		return "kill_switch_invalid"
	}
}

func waitContext(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return context.Canceled
	}
}

func environmentWithPythonPath(sourceRoot string) []string {
	environment := make([]string, 0, len(os.Environ())+1)
	existing := ""
	for _, entry := range os.Environ() {
		if strings.HasPrefix(entry, "PYTHONPATH=") {
			existing = strings.TrimPrefix(entry, "PYTHONPATH=")
			continue
		}
		environment = append(environment, entry)
	}
	if existing != "" {
		sourceRoot += string(os.PathListSeparator) + existing
	}
	return append(environment, "PYTHONPATH="+sourceRoot)
}

func normalizedNames(values []string) []string {
	result := make([]string, len(values))
	copy(result, values)
	sort.Strings(result)
	return result
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func intersectStrings(left, right []string) []string {
	result := make([]string, 0)
	leftIndex := 0
	rightIndex := 0
	for leftIndex < len(left) && rightIndex < len(right) {
		switch {
		case left[leftIndex] < right[rightIndex]:
			leftIndex++
		case left[leftIndex] > right[rightIndex]:
			rightIndex++
		default:
			result = append(result, left[leftIndex])
			leftIndex++
			rightIndex++
		}
	}
	return result
}

var _ io.Writer = (*boundedBuffer)(nil)
