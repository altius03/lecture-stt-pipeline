package shadow

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func writeFakePythonProbe(t *testing.T, root, watchFolder, scanPayload string) string {
	t.Helper()
	scanResults := strings.Split(scanPayload, "\n")
	if len(scanResults) != 2 {
		t.Fatalf("fake lockstep probe requires exactly two result lines")
	}

	scriptPath := filepath.Join(root, "fake-python")
	script := fmt.Sprintf(`#!/usr/bin/env bash
set -euo pipefail

if [ "$1" != "-m" ]; then
  exit 2
fi

if [ "$2" = "lecture_stt.stt.shadow_probe" ]; then
  if [ "$3" = "config" ]; then
    cat <<'JSON'
{"schema_version":"%s","watch_folder":"%s","stable_for_sec":0,"polling_interval_sec":1}
JSON
  elif [ "$3" = "lockstep-scan" ]; then
    IFS= read -r request_one
    cat <<'JSON'
%s
JSON
    IFS= read -r request_two
    cat <<'JSON'
%s
JSON
  else
    exit 2
  fi
  exit 0
fi

if [ "$2" = "lecture_stt.stt.main" ]; then
  # This fake probe intentionally does not support live plan introspection unless
  # a concrete positive integration path asks for it.
  exit 0
fi

exit 2
`, ConfigSchemaVersion, watchFolder, scanResults[0], scanResults[1])
	if err := os.WriteFile(scriptPath, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	return scriptPath
}

func TestDecodeShadowConfigRejectsDuplicateOrUnknownJSONFields(t *testing.T) {
	root := t.TempDir()
	configPayload := fmt.Sprintf(`{"schema_version":"%s","watch_folder":"%s","stable_for_sec":0,"polling_interval_sec":1,"watch_folder":"%s"}`, ConfigSchemaVersion, root, root)
	_, err := decodeShadowConfig([]byte(configPayload))
	if err == nil {
		t.Fatal("expected duplicate key rejection in config payload")
	}
}

func TestNewFailureReportIncludesClosedRunMetadata(t *testing.T) {
	report := NewFailureReport("invalid_options")

	assertRunMetadata(t, report)
	if report.SchemaVersion != ReportSchemaVersion ||
		report.Mode != "read_only" ||
		report.ErrorKind != "invalid_options" ||
		report.OK {
		t.Fatalf("unexpected failure report: %#v", report)
	}
	if report.PlanChecks == nil || report.ScanComparisons == nil {
		t.Fatal("failure report arrays must remain closed empty arrays")
	}
}

func TestDecodeProbeScanReportRejectsDuplicateOrUnknownFields(t *testing.T) {
	config := ShadowConfig{
		SchemaVersion:      ConfigSchemaVersion,
		WatchFolder:        t.TempDir(),
		StableForSec:       0,
		PollingIntervalSec: 1,
	}
	reportPayload := probeScanReport{
		SchemaVersion:      ScanSchemaVersion,
		PollingIntervalSec: 1,
		ScanCount:          1,
		Scans:              []probeScan{{ScanIndex: 1}},
		SleepSec:           1,
		StableForSec:       0,
	}
	raw, err := json.Marshal(reportPayload)
	if err != nil {
		t.Fatal(err)
	}
	var wrapper map[string]any
	if err := json.Unmarshal(raw, &wrapper); err != nil {
		t.Fatal(err)
	}
	wrapper["unexpected"] = true
	encoded, err := json.Marshal(wrapper)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeProbeScanReport(encoded, config, 1, 1); err == nil {
		t.Fatal("expected unknown scan field rejection")
	}
}

func TestDecodeProbeScanReportRejectsUnknownFieldPayload(t *testing.T) {
	config := ShadowConfig{
		SchemaVersion:      ConfigSchemaVersion,
		WatchFolder:        t.TempDir(),
		StableForSec:       0,
		PollingIntervalSec: 1,
	}
	// Duplicate key payloads are rejected by rejectDuplicateJSONKeys.
	payload := []byte(`{"schema_version":"lecture-stt/shadow-scan@1","watch_folder":"/tmp/watch","polling_interval_sec":1,"scan_count":1,"stable_for_sec":0,"sleep_sec":0,"scans":[{"scan_index":1,"stable_relative_paths":[]}]}`)
	if _, err := decodeProbeScanReport(payload, config, 1, 0); err == nil {
		t.Fatal("expected unknown/invalid scan payload rejection")
	}
}

func TestResolveScanScheduleHonorsClosedBoundsAndDefaultCount(t *testing.T) {
	config := ShadowConfig{
		SchemaVersion:      ConfigSchemaVersion,
		WatchFolder:        t.TempDir(),
		StableForSec:       10,
		PollingIntervalSec: 4,
	}

	scanCount, sleepSec, err := resolveScanSchedule(Options{ScanCount: 0}, config)
	if err != nil {
		t.Fatalf("resolveScanSchedule() failed: %v", err)
	}
	if scanCount != 5 || sleepSec != 4 {
		t.Fatalf("expected bounded count 5 and sleep 4, got count=%d sleep=%.3f", scanCount, sleepSec)
	}

	scanCount, sleepSec, err = resolveScanSchedule(Options{ScanCount: maxScanCount}, config)
	if err != nil {
		t.Fatalf("resolveScanSchedule() failed with explicit max count: %v", err)
	}
	if scanCount != maxScanCount || sleepSec != 4 {
		t.Fatalf("expected explicit count %d, got %d", maxScanCount, scanCount)
	}
}

func TestResolveScanScheduleRejectsOutOfRangeCount(t *testing.T) {
	config := ShadowConfig{
		SchemaVersion:      ConfigSchemaVersion,
		WatchFolder:        t.TempDir(),
		StableForSec:       10,
		PollingIntervalSec: 4,
	}
	if _, _, err := resolveScanSchedule(Options{ScanCount: maxScanCount + 1}, config); err == nil {
		t.Fatal("expected resolveScanSchedule to reject oversized scan-count")
	}
	config.PollingIntervalSec = 3601
	if _, _, err := resolveScanSchedule(Options{ScanCount: 1}, config); err == nil {
		t.Fatal("expected resolveScanSchedule to reject oversized config polling interval")
	}
}

func TestRunSetsShadowMismatchWhenScanContractIsNotIdentical(t *testing.T) {
	repoRoot := t.TempDir()
	pythonRoot := filepath.Join(repoRoot, "src", "lecture_stt")
	if err := os.MkdirAll(pythonRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	watchFolder := t.TempDir()
	if err := os.WriteFile(filepath.Join(watchFolder, "audio.m4a"), []byte("audio"), 0o600); err != nil {
		t.Fatal(err)
	}

	scanReport := `{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":[]}
{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":2,"stable_relative_paths":[]}` // Python reports no stable files.

	options := Options{
		PythonBin:           writeFakePythonProbe(t, repoRoot, watchFolder, scanReport),
		RepoRoot:            repoRoot,
		ConfigPath:          filepath.Join(repoRoot, "config.yaml"),
		WatchFolderOverride: watchFolder,
		ScanCount:           2,
	}
	sleep := 0.0
	options.SleepSec = &sleep

	if err := os.WriteFile(options.ConfigPath, []byte("app:\n  polling_interval_sec: 1\n  stable_for_sec: 0\npaths:\n  watch_folder: .\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	report, err := Run(options)
	if err != nil {
		t.Fatalf("Run() should not fail when mismatch is detected: %v", err)
	}

	if report.OK {
		t.Fatal("expected fail-closed mismatch report")
	}
	if report.ErrorKind != "shadow_mismatch" {
		t.Fatalf("expected error_kind shadow_mismatch, got %q", report.ErrorKind)
	}
	if len(report.ScanComparisons) != 2 {
		t.Fatalf("expected 2 scan comparisons, got %d", len(report.ScanComparisons))
	}
	if report.ScanComparisons[1].Match {
		t.Fatal("expected second scan comparison to fail")
	}
	if report.VerifiedCount != 0 {
		t.Fatalf("expected no verified plans when no intersected candidates, got %d", report.VerifiedCount)
	}
	assertRunMetadata(t, report)
	secondReport, secondErr := Run(options)
	if secondErr != nil {
		t.Fatalf("second Run() should preserve report metadata contract: %v", secondErr)
	}
	assertRunMetadata(t, secondReport)
	if report.RunID == secondReport.RunID {
		t.Fatal("separate shadow invocations must have unique run ids")
	}

	encoded, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), watchFolder) {
		t.Fatalf("report should not leak watch folder path")
	}
}

func assertRunMetadata(t *testing.T, report Report) {
	t.Helper()
	runID, err := hex.DecodeString(report.RunID)
	if err != nil || len(runID) != 16 {
		t.Fatalf("expected a lowercase 128-bit run id, got %q", report.RunID)
	}
	if report.RunID != strings.ToLower(report.RunID) {
		t.Fatalf("run id must be lowercase: %q", report.RunID)
	}
	startedAt, err := time.Parse(time.RFC3339Nano, report.StartedAt)
	if err != nil {
		t.Fatalf("invalid started_at: %v", err)
	}
	completedAt, err := time.Parse(time.RFC3339Nano, report.CompletedAt)
	if err != nil {
		t.Fatalf("invalid completed_at: %v", err)
	}
	if completedAt.Before(startedAt) {
		t.Fatalf("completed_at precedes started_at: start=%s complete=%s", startedAt, completedAt)
	}
}

func TestCheckRunBoundaryTreatsKillSwitchAsFailClosed(t *testing.T) {
	root := t.TempDir()
	marker := filepath.Join(root, "controller.disabled")

	if err := checkRunBoundary(context.Background(), marker); err != nil {
		t.Fatalf("absent marker should allow read-only shadow: %v", err)
	}
	if err := os.WriteFile(marker, []byte("disabled\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := checkRunBoundary(context.Background(), marker); !errors.Is(err, errKillSwitchActive) {
		t.Fatalf("expected active kill switch, got %v", err)
	}
	if err := os.Remove(marker); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(root, "target")
	if err := os.WriteFile(target, []byte("target"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, marker); err != nil {
		t.Fatal(err)
	}
	if err := checkRunBoundary(context.Background(), marker); !errors.Is(err, errKillSwitchInvalid) {
		t.Fatalf("expected symlink kill-switch path rejection, got %v", err)
	}
}

func TestRunWithScanHookStopsWhenKillSwitchAppears(t *testing.T) {
	repoRoot := t.TempDir()
	pythonRoot := filepath.Join(repoRoot, "src", "lecture_stt")
	if err := os.MkdirAll(pythonRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	watchFolder := t.TempDir()
	marker := filepath.Join(t.TempDir(), "controller.disabled")
	scanReport := `{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":1,"stable_relative_paths":[]}
{"schema_version":"lecture-stt/shadow-scan-result@1","scan_index":2,"stable_relative_paths":[]}`
	options := Options{
		PythonBin:           writeFakePythonProbe(t, repoRoot, watchFolder, scanReport),
		RepoRoot:            repoRoot,
		ConfigPath:          filepath.Join(repoRoot, "config.yaml"),
		WatchFolderOverride: watchFolder,
		KillSwitchPath:      marker,
		ScanCount:           2,
	}
	sleep := 0.0
	options.SleepSec = &sleep
	if err := os.WriteFile(options.ConfigPath, []byte("app:\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	report, err := runWithScanHook(options, func(scanIndex int, _ string) error {
		if scanIndex == 1 {
			return os.WriteFile(marker, []byte("disabled\n"), 0o600)
		}
		return nil
	})
	if !errors.Is(err, errKillSwitchActive) {
		t.Fatalf("expected kill-switch activation, got %v", err)
	}
	if report.ErrorKind != "kill_switch_active" || report.OK {
		t.Fatalf("unexpected fail-closed report: %#v", report)
	}
	if !report.KillSwitchConfigured {
		t.Fatal("report must state that a kill switch was configured")
	}
	if len(report.ScanComparisons) != 1 {
		t.Fatalf("expected exactly one completed scan before stop, got %d", len(report.ScanComparisons))
	}
}

func TestRunPythonContextCancellationReapsProcessGroup(t *testing.T) {
	root := t.TempDir()
	pidPath := filepath.Join(root, "python.pid")
	scriptPath := filepath.Join(root, "fake-python")
	script := fmt.Sprintf(`#!/bin/sh
echo $$ > %q
while :; do sleep 60; done
`, pidPath)
	if err := os.WriteFile(scriptPath, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	resultDone := make(chan error, 1)
	started := time.Now()
	go func() {
		_, runErr := runPythonContext(
			ctx,
			Options{PythonBin: scriptPath, RepoRoot: root},
			1024,
			"ignored.module",
		)
		resultDone <- runErr
	}()
	deadline := time.Now().Add(2 * time.Second)
	for {
		if value, readErr := os.ReadFile(pidPath); readErr == nil &&
			len(strings.TrimSpace(string(value))) > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("fake Python did not publish its pid")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	err := <-resultDone
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context cancellation, got %v", err)
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("canceled subprocess cleanup exceeded bound: %s", elapsed)
	}
	rawPID, err := os.ReadFile(pidPath)
	if err != nil {
		t.Fatalf("fake Python did not publish its pid: %v", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(rawPID)))
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
		t.Fatalf("canceled Python child still exists: pid=%d err=%v", pid, err)
	}
}

func TestScanWatchFolderContextTimesOutAndReapsPythonStatScan(t *testing.T) {
	root := t.TempDir()
	pidPath := filepath.Join(root, "helper.pid")
	helper := filepath.Join(root, "scan-helper")
	script := fmt.Sprintf(`#!/bin/sh
echo $$ > %q
while :; do sleep 60; done
`, pidPath)
	if err := os.WriteFile(helper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	_, err := scanWatchFolderContext(
		context.Background(),
		Options{
			PythonBin:      helper,
			PythonStatScan: true,
			ScanTimeout:    500 * time.Millisecond,
		},
		root,
	)
	if err == nil {
		t.Fatal("expected bounded Python stat scan failure")
	}
	if elapsed := time.Since(started); elapsed > 2*time.Second {
		t.Fatalf("scan helper timeout exceeded bound: %s", elapsed)
	}
	rawPID, err := os.ReadFile(pidPath)
	if err != nil {
		t.Fatalf("scan helper did not publish pid: %v", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(rawPID)))
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
		t.Fatalf("timed-out scan helper still exists: pid=%d err=%v", pid, err)
	}
}

func TestScanWatchFolderContextRetriesTransientPythonStatScanFailure(t *testing.T) {
	root := t.TempDir()
	counter := filepath.Join(root, "counter")
	helper := filepath.Join(root, "scan-helper")
	script := fmt.Sprintf(`#!/bin/sh
count=0
if [ -f %q ]; then count=$(cat %q); fi
count=$((count + 1))
printf '%%s' "$count" > %q
if [ "$count" -lt 3 ]; then exit 2; fi
printf '{"entries":[],"schema_version":"lecture-stt/controller-stat-scan@1"}\n'
`, counter, counter, counter)
	if err := os.WriteFile(helper, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	entries, err := scanWatchFolderContext(
		context.Background(),
		Options{
			PythonBin:          helper,
			PythonStatScan:     true,
			ScanHelperAttempts: 3,
			ScanTimeout:        time.Second,
		},
		root,
	)
	if err != nil {
		t.Fatalf("transient helper should recover on bounded retry: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("unexpected helper entries: %#v", entries)
	}
	value, err := os.ReadFile(counter)
	if err != nil {
		t.Fatal(err)
	}
	if string(value) != "3" {
		t.Fatalf("expected exactly three bounded attempts, got %q", value)
	}
}

func TestScanHelperPayloadRejectsDuplicateAndInvalidEntries(t *testing.T) {
	duplicate := []byte(`{"entries":[{"name":"a.m4a","size_bytes":1,"mtime":1},{"name":"a.m4a","size_bytes":1,"mtime":1}],"schema_version":"lecture-stt/controller-stat-scan@1"}`)
	if _, err := decodeStatScanPayload(duplicate); err == nil {
		t.Fatal("expected duplicate helper entry rejection")
	}
	invalid := []byte(`{"entries":[{"name":"../a.m4a","size_bytes":1,"mtime":1}],"schema_version":"lecture-stt/controller-stat-scan@1"}`)
	if _, err := decodeStatScanPayload(invalid); err == nil {
		t.Fatal("expected invalid helper entry rejection")
	}
}
