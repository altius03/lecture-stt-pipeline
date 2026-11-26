package shadow

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeFakeOwnershipPython(
	t *testing.T,
	root,
	watchFolder string,
	plan Plan,
	nextRetryPayload string,
	holdSingleExec bool,
) string {
	t.Helper()
	rawPlan, err := json.Marshal(plan)
	if err != nil {
		t.Fatal(err)
	}
	scriptPath := filepath.Join(root, "fake-ownership-python")
	hold := "0"
	if holdSingleExec {
		hold = "1"
	}
	script := fmt.Sprintf(`#!/usr/bin/env bash
set -euo pipefail

if [ "$1" != "-m" ]; then
  exit 2
fi
module="$2"
shift 2

if [ "$module" = "lecture_stt.stt.shadow_probe" ]; then
  if [ "$1" = "config" ]; then
    cat <<'JSON'
{"schema_version":"%s","watch_folder":"%s","stable_for_sec":0,"polling_interval_sec":1}
JSON
  elif [ "$1" = "lockstep-scan" ]; then
    IFS= read -r request_one
    cat <<'JSON'
{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":[]}
JSON
    IFS= read -r request_two
    cat <<'JSON'
{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":2,"stable_relative_paths":["audio.m4a"]}
JSON
  else
    exit 2
  fi
  exit 0
fi

if [ "$module" = "lecture_stt.stt.main" ]; then
  for arg in "$@"; do
    if [ "$arg" = "--plan-next-retry-job" ]; then
      cat <<'JSON'
%s
JSON
      exit 0
    fi
    if [ "$arg" = "--plan-single-job" ]; then
      cat <<'JSON'
%s
JSON
      exit 0
    fi
  done

  manifest=""
  kind="single"
  plan_sha=""
  prior=""
  for arg in "$@"; do
    if [ "$prior" = "--single-job-manifest" ]; then
      manifest="$arg"
      kind="single"
    fi
    if [ "$prior" = "--retry-job-manifest" ]; then
      manifest="$arg"
      kind="retry"
    fi
    if [ "$prior" = "--expected-plan-sha256" ]; then
      plan_sha="$arg"
    fi
    prior="$arg"
  done

  if [ -n "$manifest" ] && [ "$kind" = "single" ] && [ "$(cat "$manifest")" = '%s' ]; then
    if [ "%s" = "1" ]; then
      sleep 5
    fi
    printf '{"schema_version":"lecture-stt/single-job-result@1","expected_count":1,"plan_sha256":"%%s","status":"completed","job_id":1}\n' "$plan_sha"
    exit 0
  fi

  if [ -n "$manifest" ] && [ "$kind" = "retry" ] && [ -n "$plan_sha" ]; then
    printf '{"schema_version":"lecture-stt/retry-job-result@1","expected_count":1,"plan_sha256":"%%s","status":"completed","job_id":1}\n' "$plan_sha"
    exit 0
  fi
fi

exit 2
`, ConfigSchemaVersion, watchFolder, nextRetryPayload, string(rawPlan), string(rawPlan), hold)
	if err := os.WriteFile(scriptPath, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	return scriptPath
}

func ownershipOptionsForFileWithSingleHold(
	t *testing.T,
	nextRetryPayload string,
	holdSingleExec bool,
) OwnershipOptions {
	t.Helper()
	root := t.TempDir()
	watchFolder := filepath.Join(root, "watch")
	stateDir := filepath.Join(root, "state")
	if err := os.MkdirAll(filepath.Join(root, "src", "lecture_stt"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(watchFolder, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(stateDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(watchFolder, "audio.m4a"), []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}
	plan := validPlanForFile(t, watchFolder, "audio.m4a")
	pythonBin := writeFakeOwnershipPython(t, root, watchFolder, plan, nextRetryPayload, holdSingleExec)
	configPath := filepath.Join(root, "config.yaml")
	if err := os.WriteFile(configPath, []byte("app:\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	sleep := 0.0
	return OwnershipOptions{
		Shadow: Options{
			PythonBin:      pythonBin,
			RepoRoot:       root,
			ConfigPath:     configPath,
			KillSwitchPath: filepath.Join(root, "controller.disabled"),
			ScanCount:      2,
			SleepSec:       &sleep,
		},
		StateDir:     stateDir,
		Enable:       true,
		AllowWrite:   true,
		MaxFreshJobs: 1,
	}
}

func ownershipOptionsForFile(t *testing.T, nextRetryPayload string) OwnershipOptions {
	return ownershipOptionsForFileWithSingleHold(t, nextRetryPayload, false)
}

func TestRunOwnershipCycleExecutesOnlyReverifiedExactSinglePlan(t *testing.T) {
	options := ownershipOptionsForFile(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null}`,
	)

	report, err := RunOwnershipCycleContext(context.Background(), options)
	if err != nil {
		t.Fatalf("RunOwnershipCycleContext() failed: %v", err)
	}
	if !report.OK || report.Mode != "controller_owned" || !report.PollingMatch {
		t.Fatalf("unexpected ownership report: %#v", report)
	}
	if report.ObservationMode != "go_filesystem_scan_go_stability_tracker" ||
		report.PlanVerification != "go_contract_and_live_source_then_python_replan_apply" {
		t.Fatalf("unexpected ownership evidence modes: %#v", report)
	}
	if report.PlanCount != 1 || report.ExecutionCount != 1 || report.RetryPlanned {
		t.Fatalf("unexpected ownership counts: %#v", report)
	}
	if len(report.Executions) != 1 ||
		report.Executions[0] != (OwnershipExecution{Kind: "single", Status: "completed"}) {
		t.Fatalf("unexpected execution summary: %#v", report.Executions)
	}
	entries, err := os.ReadDir(options.StateDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.Contains(entry.Name(), "manifest") {
			t.Fatalf("owned manifest was not cleaned up: %s", entry.Name())
		}
	}
}

func TestRunOwnershipCycleExecutesRetryPlanBeforeSinglePlan(t *testing.T) {
	const retryPlanSHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	options := ownershipOptionsForFileWithSingleHold(
		t,
		fmt.Sprintf(
			`{"schema_version":"lecture-stt/controller-next-retry@1","status":"planned","plan":{"schema_version":"lecture-stt/retry-job-plan@1","plan_sha256":"%s"}}`,
			retryPlanSHA,
		),
		false,
	)

	report, err := RunOwnershipCycleContext(context.Background(), options)
	if err != nil {
		t.Fatalf("RunOwnershipCycleContext() failed: %v", err)
	}
	if len(report.Executions) != 2 {
		t.Fatalf("expected retry + single executions, got %#v", report.Executions)
	}
	if report.Executions[0].Kind != "retry" || report.Executions[0].Status != "completed" {
		t.Fatalf("retry execution should be first and completed: %#v", report.Executions)
	}
	if report.Executions[1].Kind != "single" || report.Executions[1].Status != "completed" {
		t.Fatalf("single execution should follow retry and be completed: %#v", report.Executions)
	}
	if !report.RetryPlanned || report.ExecutionCount != 2 || report.PlanCount != 1 {
		t.Fatalf("unexpected ownership execution totals: %#v", report)
	}
}

func TestRunOwnershipCycleTreatsActiveKillSwitchAsIdleWithoutExecution(t *testing.T) {
	options := ownershipOptionsForFile(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null}`,
	)
	if err := os.WriteFile(options.Shadow.KillSwitchPath, []byte("disabled\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	report, err := RunOwnershipCycleContext(context.Background(), options)
	if err != nil {
		t.Fatalf("active kill switch should keep the controller idle: %v", err)
	}
	if !report.OK || !report.KillSwitchActive || report.ExecutionCount != 0 {
		t.Fatalf("unexpected kill-switch report: %#v", report)
	}
}

func TestNormalizeOwnershipOptionsRequiresClosedWriteGuardsAndOwnedState(t *testing.T) {
	options := ownershipOptionsForFile(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null}`,
	)
	options.Enable = false
	if _, err := normalizeOwnershipOptions(options); err == nil {
		t.Fatal("expected disabled ownership to fail closed")
	}
	options.Enable = true
	if err := os.Chmod(options.StateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := normalizeOwnershipOptions(options); err == nil {
		t.Fatal("expected permissive state directory to fail closed")
	}
}

func TestRunOwnershipCycleCancelsSingleExecutionAndCleansManifest(t *testing.T) {
	options := ownershipOptionsForFileWithSingleHold(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null}`,
		true,
	)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) && ctx.Err() == nil {
			entries, err := os.ReadDir(options.StateDir)
			if err != nil {
				continue
			}
			for _, entry := range entries {
				if strings.Contains(entry.Name(), "single-manifest") {
					cancel()
					return
				}
			}
			time.Sleep(5 * time.Millisecond)
		}
	}()

	report, err := RunOwnershipCycleContext(ctx, options)
	if err == nil || !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context canceled, got err=%v", err)
	}
	if report.ErrorKind != "single_job_execution_failed" || report.ExecutionCount != 0 {
		t.Fatalf("unexpected cancellation report: %#v", report)
	}
	entries, err := os.ReadDir(options.StateDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.Contains(entry.Name(), "manifest") {
			t.Fatalf("owned manifest was not cleaned up after cancellation: %s", entry.Name())
		}
	}
}

func TestRunOwnershipCycleRejectsMalformedNextRetryEnvelope(t *testing.T) {
	options := ownershipOptionsForFile(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null,"unexpected":true}`,
	)
	report, err := RunOwnershipCycleContext(context.Background(), options)
	if err == nil {
		t.Fatal("expected malformed retry envelope to fail closed")
	}
	if report.ErrorKind != "retry_execution_failed" || report.ExecutionCount != 0 {
		t.Fatalf("unexpected malformed retry report: %#v", report)
	}
}

func TestRunOwnershipCycleRecoversOwnedStaleManifestBeforePlanning(t *testing.T) {
	options := ownershipOptionsForFile(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null}`,
	)
	stale := filepath.Join(options.StateDir, ".single-manifest-stale")
	if err := os.WriteFile(stale, []byte(`{"stale":true}`), 0o600); err != nil {
		t.Fatal(err)
	}

	report, err := RunOwnershipCycleContext(context.Background(), options)
	if err != nil {
		t.Fatalf("owned stale manifest should be safely recovered: %v", err)
	}
	if report.RecoveredManifests != 1 || !report.OK {
		t.Fatalf("unexpected recovery report: %#v", report)
	}
	if _, err := os.Lstat(stale); !os.IsNotExist(err) {
		t.Fatalf("stale manifest still exists: %v", err)
	}
}

func TestRunOwnershipCycleRejectsUnknownStateEntry(t *testing.T) {
	options := ownershipOptionsForFile(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null}`,
	)
	if err := os.WriteFile(filepath.Join(options.StateDir, "foreign"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	report, err := RunOwnershipCycleContext(context.Background(), options)
	if err == nil || report.ErrorKind != "manifest_recovery_failed" {
		t.Fatalf("unknown state entry must fail closed: report=%#v err=%v", report, err)
	}
}

func TestRunOwnershipCycleRejectsUnsafeControllerLock(t *testing.T) {
	options := ownershipOptionsForFile(
		t,
		`{"schema_version":"lecture-stt/controller-next-retry@1","status":"empty","plan":null}`,
	)
	target := filepath.Join(t.TempDir(), "target")
	if err := os.WriteFile(target, []byte("do not touch"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(options.StateDir, "controller.lock")); err != nil {
		t.Fatal(err)
	}
	report, err := RunOwnershipCycleContext(context.Background(), options)
	if err == nil || report.ErrorKind != "manifest_recovery_failed" {
		t.Fatalf("unsafe controller lock must fail closed: report=%#v err=%v", report, err)
	}
	value, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(value) != "do not touch" {
		t.Fatalf("unsafe lock target was modified: %q", value)
	}
}
