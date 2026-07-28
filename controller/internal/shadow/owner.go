package shadow

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const (
	OwnershipReportSchemaVersion = "lecture-stt/controller-ownership-report@2"
	nextRetrySchemaVersion       = "lecture-stt/controller-next-retry@1"
	singleJobResultSchemaVersion = "lecture-stt/single-job-result@1"
	retryJobResultSchemaVersion  = "lecture-stt/retry-job-result@1"
	maxOwnedResultBytes          = 64 * 1024
)

type OwnershipOptions struct {
	Shadow       Options
	StateDir     string
	Enable       bool
	AllowWrite   bool
	MaxFreshJobs int
}

type OwnershipExecution struct {
	Kind   string `json:"kind"`
	Status string `json:"status"`
}

type OwnershipReport struct {
	CompletedAt        string               `json:"completed_at"`
	ErrorKind          string               `json:"error_kind,omitempty"`
	ExecutionCount     int                  `json:"execution_count"`
	Executions         []OwnershipExecution `json:"executions"`
	KillSwitchActive   bool                 `json:"kill_switch_active"`
	Mode               string               `json:"mode"`
	ObservationMode    string               `json:"observation_mode"`
	OK                 bool                 `json:"ok"`
	PlanCount          int                  `json:"plan_count"`
	PlanVerification   string               `json:"plan_verification"`
	PollingMatch       bool                 `json:"polling_match"`
	RetryPlanned       bool                 `json:"retry_planned"`
	RecoveredManifests int                  `json:"recovered_manifests"`
	SchemaVersion      string               `json:"schema_version"`
	StartedAt          string               `json:"started_at"`
}

type ownedResult struct {
	SchemaVersion string `json:"schema_version"`
	ExpectedCount int    `json:"expected_count"`
	PlanSHA256    string `json:"plan_sha256"`
	Status        string `json:"status"`
	JobID         *int   `json:"job_id"`
}

type nextRetryEnvelope struct {
	SchemaVersion string          `json:"schema_version"`
	Status        string          `json:"status"`
	Plan          json.RawMessage `json:"plan"`
}

type retryPlanDigest struct {
	SchemaVersion string `json:"schema_version"`
	PlanSHA256    string `json:"plan_sha256"`
}

func RunOwnershipCycleContext(
	ctx context.Context,
	options OwnershipOptions,
) (report OwnershipReport, runErr error) {
	startedAt := time.Now().UTC()
	report = OwnershipReport{
		SchemaVersion:    OwnershipReportSchemaVersion,
		Mode:             "controller_owned",
		ObservationMode:  ownershipObservationMode(options.Shadow),
		PlanVerification: ownershipPlanVerification(options.Shadow),
		Executions:       []OwnershipExecution{},
		StartedAt:        startedAt.Format(time.RFC3339Nano),
	}
	defer func() {
		completedAt := time.Now().UTC()
		if completedAt.Before(startedAt) {
			completedAt = startedAt
		}
		report.CompletedAt = completedAt.Format(time.RFC3339Nano)
	}()

	normalized, err := normalizeOwnershipOptions(options)
	if err != nil {
		report.ErrorKind = "invalid_options"
		return report, err
	}
	if err := checkRunBoundary(ctx, normalized.Shadow.KillSwitchPath); err != nil {
		if errors.Is(err, errKillSwitchActive) {
			report.KillSwitchActive = true
			report.OK = true
			return report, nil
		}
		report.ErrorKind = boundaryErrorKind(err)
		return report, err
	}
	recovered, err := recoverOwnedManifests(normalized.StateDir)
	if err != nil {
		report.ErrorKind = "manifest_recovery_failed"
		return report, err
	}
	report.RecoveredManifests = recovered

	retryExecution, retryPlanned, err := executeNextRetryContext(ctx, normalized)
	report.RetryPlanned = retryPlanned
	if err != nil {
		report.ErrorKind = "retry_execution_failed"
		return report, err
	}
	if retryPlanned {
		report.Executions = append(report.Executions, retryExecution)
		report.ExecutionCount++
	}

	if err := checkRunBoundary(ctx, normalized.Shadow.KillSwitchPath); err != nil {
		if errors.Is(err, errKillSwitchActive) {
			report.KillSwitchActive = true
			report.OK = true
			return report, nil
		}
		report.ErrorKind = boundaryErrorKind(err)
		return report, err
	}

	shadowReport, err := RunContext(ctx, normalized.Shadow)
	report.PollingMatch = shadowReport.PollingMatch
	report.PlanCount = len(shadowReport.PlanChecks)
	if err != nil {
		if shadowReport.ErrorKind == "kill_switch_active" {
			report.KillSwitchActive = true
			report.OK = true
			return report, nil
		}
		report.ErrorKind = shadowReport.ErrorKind
		if report.ErrorKind == "" {
			report.ErrorKind = "shadow_failed"
		}
		return report, err
	}
	if !shadowReport.OK {
		report.ErrorKind = "shadow_mismatch"
		return report, errors.New("controller ownership shadow gate did not pass")
	}
	watchFolder, err := resolveOwnershipWatchFolderContext(ctx, normalized.Shadow)
	if err != nil {
		report.ErrorKind = "config_probe_failed"
		return report, err
	}

	executed := 0
	seen := make(map[string]struct{}, len(shadowReport.PlanChecks))
	for _, check := range shadowReport.PlanChecks {
		if check.Status != "verified" {
			report.ErrorKind = "unverified_plan"
			return report, errors.New("controller ownership received an unverified plan")
		}
		if _, exists := seen[check.RelativePath]; exists {
			continue
		}
		seen[check.RelativePath] = struct{}{}
		if executed >= normalized.MaxFreshJobs {
			break
		}
		if err := checkRunBoundary(ctx, normalized.Shadow.KillSwitchPath); err != nil {
			if errors.Is(err, errKillSwitchActive) {
				report.KillSwitchActive = true
				report.OK = true
				return report, nil
			}
			report.ErrorKind = boundaryErrorKind(err)
			return report, err
		}
		latest, rawPlan := buildVerifiedPythonPlanContext(
			ctx,
			normalized.Shadow,
			watchFolder,
			check.RelativePath,
		)
		if latest.Status != "verified" || latest.PlanSHA256 != check.PlanSHA256 || len(rawPlan) == 0 {
			report.ErrorKind = "stale_plan"
			return report, errors.New("single-job plan changed before controller execution")
		}
		execution, err := executeOwnedPlanContext(
			ctx,
			normalized,
			"single",
			rawPlan,
			check.PlanSHA256,
		)
		if err != nil {
			report.ErrorKind = "single_job_execution_failed"
			return report, err
		}
		report.Executions = append(report.Executions, execution)
		report.ExecutionCount++
		executed++
	}
	report.OK = true
	return report, nil
}

func ownershipObservationMode(options Options) string {
	if options.PythonStatScan {
		return "python_stat_projection_go_stability_tracker"
	}
	return "go_filesystem_scan_go_stability_tracker"
}

func ownershipPlanVerification(options Options) string {
	if options.PythonStatScan {
		return "go_contract_then_python_live_replan_apply"
	}
	return "go_contract_and_live_source_then_python_replan_apply"
}

func normalizeOwnershipOptions(options OwnershipOptions) (OwnershipOptions, error) {
	if !options.Enable || !options.AllowWrite {
		return OwnershipOptions{}, errors.New("controller ownership requires explicit execution and write guards")
	}
	if strings.TrimSpace(options.Shadow.KillSwitchPath) == "" {
		return OwnershipOptions{}, errors.New("controller ownership requires a kill-switch path")
	}
	shadowOptions, err := normalizeOptions(options.Shadow)
	if err != nil {
		return OwnershipOptions{}, err
	}
	stateDir, err := validateOwnedStateDir(options.StateDir)
	if err != nil {
		return OwnershipOptions{}, err
	}
	maxFreshJobs := options.MaxFreshJobs
	if maxFreshJobs == 0 {
		maxFreshJobs = 1
	}
	if maxFreshJobs != 1 {
		return OwnershipOptions{}, errors.New("controller ownership max-fresh-jobs must be exactly 1")
	}
	return OwnershipOptions{
		Shadow:       shadowOptions,
		StateDir:     stateDir,
		Enable:       true,
		AllowWrite:   true,
		MaxFreshJobs: maxFreshJobs,
	}, nil
}

func validateOwnedStateDir(raw string) (string, error) {
	if strings.TrimSpace(raw) == "" {
		return "", errors.New("controller state directory is required")
	}
	absolute, err := filepath.Abs(raw)
	if err != nil {
		return "", errors.New("controller state directory is invalid")
	}
	absolute = filepath.Clean(absolute)
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", errors.New("controller state directory is not safely resolvable")
	}
	resolved = filepath.Clean(resolved)
	info, err := os.Lstat(resolved)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", errors.New("controller state directory must be an existing directory")
	}
	statValue, ok := info.Sys().(*syscall.Stat_t)
	if !ok || int(statValue.Uid) != os.Geteuid() || info.Mode().Perm() != 0o700 {
		return "", errors.New("controller state directory must be owned by the current user with mode 0700")
	}
	return resolved, nil
}

func resolveOwnershipWatchFolderContext(ctx context.Context, options Options) (string, error) {
	configBytes, err := runPythonContext(
		ctx,
		options,
		maxPlanBytes,
		"lecture_stt.stt.shadow_probe",
		configArguments(options)...,
	)
	if err != nil {
		return "", errors.New("controller ownership config probe failed")
	}
	config, err := decodeShadowConfig(configBytes)
	if err != nil {
		return "", errors.New("controller ownership config contract is invalid")
	}
	return config.WatchFolder, nil
}

func executeNextRetryContext(
	ctx context.Context,
	options OwnershipOptions,
) (OwnershipExecution, bool, error) {
	value, exitCode, err := runOwnedPythonContext(
		ctx,
		options.Shadow,
		maxOwnedResultBytes,
		nil,
		"lecture_stt.stt.main",
		"--config",
		options.Shadow.ConfigPath,
		"--plan-next-retry-job",
	)
	if err != nil || exitCode != 0 {
		return OwnershipExecution{}, false, errors.New("next retry plan command failed")
	}
	if err := rejectDuplicateJSONKeys(value); err != nil {
		return OwnershipExecution{}, false, errors.New("next retry plan JSON is invalid")
	}
	var envelope nextRetryEnvelope
	if err := strictDecode(value, &envelope); err != nil {
		return OwnershipExecution{}, false, errors.New("next retry plan contract is invalid")
	}
	if envelope.SchemaVersion != nextRetrySchemaVersion {
		return OwnershipExecution{}, false, errors.New("next retry plan schema is invalid")
	}
	switch envelope.Status {
	case "empty":
		if string(envelope.Plan) != "null" {
			return OwnershipExecution{}, false, errors.New("empty retry plan must be null")
		}
		return OwnershipExecution{}, false, nil
	case "planned":
	default:
		return OwnershipExecution{}, false, errors.New("next retry plan status is invalid")
	}
	if len(envelope.Plan) == 0 || bytes.Equal(envelope.Plan, []byte("null")) {
		return OwnershipExecution{}, false, errors.New("planned retry is missing its exact plan")
	}
	if err := rejectDuplicateJSONKeys(envelope.Plan); err != nil {
		return OwnershipExecution{}, false, errors.New("retry plan JSON is invalid")
	}
	var digest retryPlanDigest
	if err := strictDecodeAllowUnknown(envelope.Plan, &digest); err != nil ||
		digest.SchemaVersion != "lecture-stt/retry-job-plan@1" ||
		!isLowerSHA256(digest.PlanSHA256) {
		return OwnershipExecution{}, false, errors.New("retry plan digest contract is invalid")
	}
	execution, err := executeOwnedPlanContext(
		ctx,
		options,
		"retry",
		envelope.Plan,
		digest.PlanSHA256,
	)
	return execution, true, err
}

func executeOwnedPlanContext(
	ctx context.Context,
	options OwnershipOptions,
	kind string,
	rawPlan []byte,
	planSHA256 string,
) (OwnershipExecution, error) {
	manifestPath, err := writeOwnedManifest(options.StateDir, kind, rawPlan)
	if err != nil {
		return OwnershipExecution{}, err
	}
	defer removeOwnedManifest(options.StateDir, manifestPath)

	manifestFlag := "--single-job-manifest"
	enableFlag := "--enable-single-job"
	expectedSchema := singleJobResultSchemaVersion
	if kind == "retry" {
		manifestFlag = "--retry-job-manifest"
		enableFlag = "--enable-retry-job"
		expectedSchema = retryJobResultSchemaVersion
	}
	args := []string{
		"--config", options.Shadow.ConfigPath,
		manifestFlag, manifestPath,
		enableFlag,
		"--controller-kill-switch", options.Shadow.KillSwitchPath,
		"--allow-write",
		"--expected-count", "1",
		"--expected-plan-sha256", planSHA256,
	}
	value, exitCode, runErr := runOwnedPythonContext(
		ctx,
		options.Shadow,
		maxOwnedResultBytes,
		nil,
		"lecture_stt.stt.main",
		args...,
	)
	if runErr != nil && exitCode != 2 && exitCode != 75 {
		return OwnershipExecution{}, runErr
	}
	if err := rejectDuplicateJSONKeys(value); err != nil {
		return OwnershipExecution{}, errors.New("owned execution result JSON is invalid")
	}
	var result ownedResult
	if err := strictDecode(value, &result); err != nil {
		return OwnershipExecution{}, errors.New("owned execution result contract is invalid")
	}
	if result.SchemaVersion != expectedSchema ||
		result.ExpectedCount != 1 ||
		result.PlanSHA256 != planSHA256 ||
		!isLowerSHA256(result.PlanSHA256) {
		return OwnershipExecution{}, errors.New("owned execution result evidence does not match the plan")
	}
	allowed := map[string]map[string]struct{}{
		"single": {
			"completed": {}, "needs_review": {}, "retry_pending": {},
			"busy": {}, "skipped": {}, "failed": {},
		},
		"retry": {
			"completed": {}, "needs_review": {}, "retry_pending": {},
			"busy": {}, "failed": {},
		},
	}
	if _, ok := allowed[kind][result.Status]; !ok {
		return OwnershipExecution{}, errors.New("owned execution result status is invalid")
	}
	if exitCode == 0 && result.Status != "completed" && result.Status != "needs_review" {
		return OwnershipExecution{}, errors.New("owned execution success exit does not match its status")
	}
	if exitCode == 75 && result.Status != "retry_pending" && result.Status != "busy" {
		return OwnershipExecution{}, errors.New("owned execution temporary exit does not match its status")
	}
	if exitCode == 2 && result.Status != "failed" && result.Status != "skipped" {
		return OwnershipExecution{}, errors.New("owned execution failure exit does not match its status")
	}
	return OwnershipExecution{Kind: kind, Status: result.Status}, nil
}

func runOwnedPythonContext(
	ctx context.Context,
	options Options,
	outputLimit int,
	stdin []byte,
	module string,
	args ...string,
) ([]byte, int, error) {
	stdout := &boundedBuffer{limit: outputLimit}
	stderr := &boundedBuffer{limit: maxPlanBytes}
	command := pythonCommand(options, module, args...)
	if stdin != nil {
		command.Stdin = bytes.NewReader(stdin)
	}
	command.Stdout = stdout
	command.Stderr = stderr
	if err := command.Start(); err != nil {
		return nil, -1, errors.New("owned Python command could not start")
	}
	waitDone := make(chan error, 1)
	go func() {
		waitDone <- command.Wait()
	}()
	var waitErr error
	select {
	case waitErr = <-waitDone:
	case <-ctx.Done():
		terminateSubprocess(command)
		<-waitDone
		return nil, 130, context.Canceled
	}
	if err := ctx.Err(); err != nil {
		return nil, 130, context.Canceled
	}
	exitCode := 0
	if waitErr != nil {
		var exitErr *exec.ExitError
		if !errors.As(waitErr, &exitErr) {
			return nil, -1, errors.New("owned Python command failed without an exit status")
		}
		exitCode = exitErr.ExitCode()
	}
	return stdout.Bytes(), exitCode, waitErr
}

func writeOwnedManifest(stateDir, kind string, raw []byte) (string, error) {
	if len(raw) == 0 || len(raw) > maxPlanBytes {
		return "", errors.New("owned manifest size is invalid")
	}
	file, err := os.CreateTemp(stateDir, "."+kind+"-manifest-")
	if err != nil {
		return "", errors.New("owned manifest could not be created")
	}
	path := file.Name()
	cleanup := func() {
		_ = file.Close()
		_ = os.Remove(path)
	}
	if err := file.Chmod(0o600); err != nil {
		cleanup()
		return "", errors.New("owned manifest mode could not be fixed")
	}
	if _, err := file.Write(raw); err != nil {
		cleanup()
		return "", errors.New("owned manifest could not be written")
	}
	if err := file.Sync(); err != nil {
		cleanup()
		return "", errors.New("owned manifest could not be synced")
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(path)
		return "", errors.New("owned manifest could not be closed")
	}
	if err := syncDirectory(stateDir); err != nil {
		_ = os.Remove(path)
		return "", err
	}
	return path, nil
}

func removeOwnedManifest(stateDir, path string) {
	if filepath.Dir(path) != stateDir ||
		(!strings.HasPrefix(filepath.Base(path), ".single-manifest-") &&
			!strings.HasPrefix(filepath.Base(path), ".retry-manifest-")) {
		return
	}
	_ = os.Remove(path)
	_ = syncDirectory(stateDir)
}

func recoverOwnedManifests(stateDir string) (int, error) {
	entries, err := os.ReadDir(stateDir)
	if err != nil {
		return 0, errors.New("controller state directory could not be inventoried")
	}
	recovered := 0
	for _, entry := range entries {
		name := entry.Name()
		if name == "controller.lock" {
			path := filepath.Join(stateDir, name)
			info, err := os.Lstat(path)
			if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 ||
				info.Mode().Perm() != 0o600 {
				return 0, errors.New("controller state directory contains an unsafe lock")
			}
			statValue, ok := info.Sys().(*syscall.Stat_t)
			if !ok || int(statValue.Uid) != os.Geteuid() || statValue.Nlink != 1 {
				return 0, errors.New("controller state directory contains an unsafe lock")
			}
			continue
		}
		if !strings.HasPrefix(name, ".single-manifest-") &&
			!strings.HasPrefix(name, ".retry-manifest-") {
			return 0, errors.New("controller state directory contains an unknown entry")
		}
		path := filepath.Join(stateDir, name)
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 ||
			info.Mode().Perm() != 0o600 {
			return 0, errors.New("controller state directory contains unsafe recovery evidence")
		}
		statValue, ok := info.Sys().(*syscall.Stat_t)
		if !ok || int(statValue.Uid) != os.Geteuid() || statValue.Nlink != 1 {
			return 0, errors.New("controller state directory contains unsafe recovery evidence")
		}
		if err := os.Remove(path); err != nil {
			return 0, errors.New("controller stale manifest could not be recovered")
		}
		recovered++
	}
	if recovered > 0 {
		if err := syncDirectory(stateDir); err != nil {
			return 0, err
		}
	}
	return recovered, nil
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return errors.New("controller state directory could not be opened for sync")
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return errors.New("controller state directory could not be synced")
	}
	return nil
}

func strictDecodeAllowUnknown(value []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(value))
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("unexpected trailing JSON value")
		}
		return err
	}
	return nil
}

func isLowerSHA256(value string) bool {
	if len(value) != 64 || strings.ToLower(value) != value {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
