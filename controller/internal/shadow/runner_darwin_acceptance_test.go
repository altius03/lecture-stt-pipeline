package shadow

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"
)

const darwinAcceptanceRuns = 20
const darwinAcceptanceVerifiedPerRun = 5

func TestRunDarwinAcceptance20ConsecutiveRuns(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("Darwin acceptance coverage only runs on macOS")
	}
	if os.Getenv("LECTURE_STT_RUN_DARWIN_ACCEPTANCE") != "1" {
		t.Skip("set LECTURE_STT_RUN_DARWIN_ACCEPTANCE=1 to run Darwin acceptance coverage")
	}

	repoRoot := locateRepositoryRoot(t)
	pythonPath := filepath.Join(repoRoot, ".venv", "bin", "python")
	pythonInfo, err := os.Stat(pythonPath)
	if err != nil || pythonInfo.IsDir() || pythonInfo.Mode()&0o111 == 0 {
		t.Skip("project .venv/python is required for Darwin acceptance coverage")
	}

	totalVerified := 0
	seenRunIDs := make(map[string]struct{}, darwinAcceptanceRuns)
	reports := make([]Report, 0, darwinAcceptanceRuns)
	var previousStartedAt time.Time
	for runIndex := 0; runIndex < darwinAcceptanceRuns; runIndex++ {
		verified, report := runDarwinAcceptanceIteration(
			t,
			repoRoot,
			pythonPath,
			runIndex,
		)
		totalVerified += verified
		reports = append(reports, report)
		assertRunMetadata(t, report)
		if _, exists := seenRunIDs[report.RunID]; exists {
			t.Fatalf("duplicate run id on acceptance run %d", runIndex+1)
		}
		seenRunIDs[report.RunID] = struct{}{}
		startedAt, err := time.Parse(time.RFC3339Nano, report.StartedAt)
		if err != nil {
			t.Fatal(err)
		}
		if !previousStartedAt.IsZero() && !startedAt.After(previousStartedAt) {
			t.Fatalf("acceptance run timestamps are not strictly increasing")
		}
		previousStartedAt = startedAt
	}
	if totalVerified < darwinAcceptanceRuns*darwinAcceptanceVerifiedPerRun {
		t.Fatalf("expected at least %d verified plan observations, got %d", darwinAcceptanceRuns*darwinAcceptanceVerifiedPerRun, totalVerified)
	}
	verifyDarwinAcceptanceLedger(t, repoRoot, pythonPath, reports)
}

func verifyDarwinAcceptanceLedger(
	t *testing.T,
	repoRoot,
	pythonPath string,
	reports []Report,
) {
	t.Helper()
	ledger := make([]byte, 0)
	for _, report := range reports {
		encoded, err := json.Marshal(report)
		if err != nil {
			t.Fatal(err)
		}
		ledger = append(ledger, encoded...)
		ledger = append(ledger, '\n')
	}
	ledgerPath := filepath.Join(t.TempDir(), "shadow-evidence.jsonl")
	if err := os.WriteFile(ledgerPath, ledger, 0o600); err != nil {
		t.Fatal(err)
	}
	command := exec.Command(
		pythonPath,
		"-m",
		"lecture_stt.stt.controller_readiness",
		"--input",
		ledgerPath,
	)
	command.Dir = repoRoot
	command.Env = environmentWithPythonPath(filepath.Join(repoRoot, "src"))
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("controller readiness verifier rejected acceptance ledger: %v\n%s", err, output)
	}
	var summary struct {
		Ready         bool   `json:"ready"`
		RunCount      int    `json:"run_count"`
		ScanCount     int    `json:"scan_count"`
		SchemaVersion string `json:"schema_version"`
		VerifiedCount int    `json:"verified_count"`
	}
	if err := json.Unmarshal(output, &summary); err != nil {
		t.Fatalf("invalid controller readiness summary: %v", err)
	}
	if summary.SchemaVersion != "lecture-stt/controller-readiness-summary@1" ||
		!summary.Ready ||
		summary.RunCount != darwinAcceptanceRuns ||
		summary.VerifiedCount != darwinAcceptanceRuns*darwinAcceptanceVerifiedPerRun ||
		summary.ScanCount != darwinAcceptanceRuns*8 {
		t.Fatalf("unexpected controller readiness summary: %#v", summary)
	}
}

func runDarwinAcceptanceIteration(
	t *testing.T,
	repoRoot,
	pythonPath string,
	runIndex int,
) (int, Report) {
	t.Helper()

	watchRoot := t.TempDir()
	audioRoot := filepath.Join(t.TempDir(), "audio")
	transcriptRoot := filepath.Join(t.TempDir(), "transcripts")
	errorRoot := filepath.Join(t.TempDir(), "errors")
	tmpRoot := filepath.Join(t.TempDir(), "tmp")
	dbPath := filepath.Join(t.TempDir(), "state", "jobs.sqlite3")
	killSwitchPath := filepath.Join(t.TempDir(), "controller.disabled")

	for _, path := range []string{watchRoot, audioRoot, transcriptRoot, errorRoot, tmpRoot, filepath.Dir(dbPath)} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(audioRoot, "seed.txt"), []byte("audio-seed"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(transcriptRoot, "seed.txt"), []byte("transcript-seed"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(errorRoot, "seed.txt"), []byte("error-seed"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tmpRoot, "seed.txt"), []byte("tmp-seed"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dbPath, []byte("seed-db"), 0o600); err != nil {
		t.Fatal(err)
	}

	initialOutputs := map[string]map[string]int64{
		"audio":      snapshotSimpleDir(audioRoot, t),
		"transcript": snapshotSimpleDir(transcriptRoot, t),
		"error":      snapshotSimpleDir(errorRoot, t),
		"tmp":        snapshotSimpleDir(tmpRoot, t),
	}
	initialDB := snapshotFile(dbPath)

	sourceSentinel := filepath.Join(watchRoot, ".watch-sentinel")
	if err := os.WriteFile(sourceSentinel, []byte("stable-root-sentinel"), 0o600); err != nil {
		t.Fatal(err)
	}
	initialWatch := snapshotSimpleDir(watchRoot, t)
	sentinelSize := initialWatch[filepath.Base(sourceSentinel)]

	alphaName := fmt.Sprintf("alpha-%02d.m4a", runIndex)
	resetName := fmt.Sprintf("reset-cycle-%02d.m4a", runIndex)
	renameBaseName := fmt.Sprintf("rename-base-%02d.m4a", runIndex)
	renameFinalName := fmt.Sprintf("rename-final-%02d.m4a", runIndex)
	unicodeName := fmt.Sprintf("문자열_%02d_🗂️.m4a", runIndex)
	transientName := fmt.Sprintf("delete-before-stable-%02d.m4a", runIndex)
	dotName := fmt.Sprintf(".ignored-%02d.m4a", runIndex)
	tildeName := fmt.Sprintf("~ignored-%02d.m4a", runIndex)
	partName := fmt.Sprintf("ignored-%02d.part", runIndex)
	tmpName := fmt.Sprintf("ignored-%02d.tmp", runIndex)

	alphaPath := filepath.Join(watchRoot, alphaName)
	resetPath := filepath.Join(watchRoot, resetName)
	renameBasePath := filepath.Join(watchRoot, renameBaseName)
	renameFinalPath := filepath.Join(watchRoot, renameFinalName)
	unicodePath := filepath.Join(watchRoot, unicodeName)
	transientPath := filepath.Join(watchRoot, transientName)
	dotPath := filepath.Join(watchRoot, dotName)
	tildePath := filepath.Join(watchRoot, tildeName)
	partPath := filepath.Join(watchRoot, partName)
	tmpPath := filepath.Join(watchRoot, tmpName)

	options := Options{
		PythonBin:           pythonPath,
		RepoRoot:            repoRoot,
		ConfigPath:          writeShadowAcceptanceConfig(t, watchRoot, audioRoot, transcriptRoot, errorRoot, tmpRoot, dbPath),
		WatchFolderOverride: watchRoot,
		KillSwitchPath:      killSwitchPath,
		ScanCount:           8,
	}
	sleep := 1.0
	options.SleepSec = &sleep

	report, err := runWithScanHook(options, func(scanIndex int, _ string) error {
		switch scanIndex {
		case 1:
			if err := os.WriteFile(alphaPath, []byte("alpha-v1"), 0o600); err != nil {
				return err
			}
			if err := ageDarwinAcceptanceFile(alphaPath); err != nil {
				return err
			}
			if err := os.WriteFile(transientPath, []byte("delete-me"), 0o600); err != nil {
				return err
			}
			for path, payload := range map[string]string{
				dotPath:   "dot-ignore",
				tildePath: "tilde-ignore",
				partPath:  "part-ignore",
				tmpPath:   "tmp-ignore",
			} {
				if err := os.WriteFile(path, []byte(payload), 0o600); err != nil {
					return err
				}
			}
			return nil
		case 2:
			if err := os.Remove(transientPath); err != nil && !os.IsNotExist(err) {
				return err
			}
			return nil
		case 3:
			if f, openErr := os.OpenFile(alphaPath, os.O_APPEND|os.O_WRONLY, 0o600); openErr == nil {
				if _, writeErr := f.WriteString("-v2"); writeErr != nil {
					_ = f.Close()
					return writeErr
				}
				if closeErr := f.Close(); closeErr != nil {
					return closeErr
				}
			} else {
				return openErr
			}
			if err := ageDarwinAcceptanceFile(alphaPath); err != nil {
				return err
			}
			if err := os.WriteFile(resetPath, []byte("reset-phase-1"), 0o600); err != nil {
				return err
			}
			return ageDarwinAcceptanceFile(resetPath)
		case 4:
			if err := os.WriteFile(resetPath, []byte("r2"), 0o600); err != nil {
				return err
			}
			return ageDarwinAcceptanceFile(resetPath)
		case 5:
			if err := os.WriteFile(renameBasePath, []byte("rename-source"), 0o600); err != nil {
				return err
			}
			return ageDarwinAcceptanceFile(renameBasePath)
		case 6:
			if err := os.Rename(renameBasePath, renameFinalPath); err != nil {
				return err
			}
			if err := os.WriteFile(unicodePath, []byte("unicode-audio"), 0o600); err != nil {
				return err
			}
			if err := ageDarwinAcceptanceFile(unicodePath); err != nil {
				return err
			}
			if err := os.Remove(alphaPath); err != nil && !os.IsNotExist(err) {
				return err
			}
			if err := os.Remove(resetPath); err != nil && !os.IsNotExist(err) {
				return err
			}
			return nil
		case 8:
			for _, path := range []string{
				resetPath,
				renameFinalPath,
				unicodePath,
				dotPath,
				tildePath,
				partPath,
				tmpPath,
			} {
				if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
					return err
				}
			}
			return nil
		default:
			return nil
		}
	})
	if err != nil {
		t.Fatalf("runWithScanHook() failed on acceptance run %d: %v", runIndex+1, err)
	}

	if report.Mode != "read_only" {
		t.Fatalf("expected read_only mode, got %q", report.Mode)
	}
	if report.ErrorKind != "" || !report.OK || !report.PollingMatch {
		t.Fatalf("expected successful acceptance report, got %#v", report)
	}
	if !report.KillSwitchConfigured {
		t.Fatal("expected acceptance report to state that a kill switch was configured")
	}
	if _, err := os.Stat(killSwitchPath); !os.IsNotExist(err) {
		t.Fatalf("kill switch marker should stay absent, stat err=%v", err)
	}
	if len(report.ScanComparisons) != 8 {
		t.Fatalf("expected 8 scan comparisons, got %d", len(report.ScanComparisons))
	}
	if report.VerifiedCount != darwinAcceptanceVerifiedPerRun {
		t.Fatalf("expected %d verified plans, got %d", darwinAcceptanceVerifiedPerRun, report.VerifiedCount)
	}
	if len(report.PlanChecks) != darwinAcceptanceVerifiedPerRun {
		t.Fatalf("expected %d plan checks, got %d", darwinAcceptanceVerifiedPerRun, len(report.PlanChecks))
	}

	expectedStable := map[int][]string{
		1: {},
		2: {alphaName},
		3: {},
		4: {alphaName},
		5: {resetName},
		6: {},
		7: {renameFinalName, unicodeName},
		8: {},
	}
	for _, comparison := range report.ScanComparisons {
		if !comparison.Match {
			t.Fatalf("expected polling parity on scan %d: %#v", comparison.ScanIndex, comparison)
		}
		if !reflect.DeepEqual(expectedStable[comparison.ScanIndex], comparison.GoStableRelativePaths) {
			t.Fatalf("unexpected Go stable paths on scan %d: got=%#v want=%#v", comparison.ScanIndex, comparison.GoStableRelativePaths, expectedStable[comparison.ScanIndex])
		}
		if !reflect.DeepEqual(comparison.GoStableRelativePaths, comparison.PythonStableRelativePaths) {
			t.Fatalf("Python/Go mismatch escaped report on scan %d: %#v", comparison.ScanIndex, comparison)
		}
	}

	expectedPlanCounts := map[string]int{
		alphaName:       2,
		resetName:       1,
		renameFinalName: 1,
		unicodeName:     1,
	}
	actualPlanCounts := map[string]int{}
	for _, check := range report.PlanChecks {
		if check.Status != "verified" || check.ErrorKind != "" {
			t.Fatalf("expected verified plan check, got %+v", check)
		}
		actualPlanCounts[check.RelativePath]++
		if check.ScanIndex == 0 {
			t.Fatalf("expected originating scan index on plan check: %+v", check)
		}
	}
	if !reflect.DeepEqual(expectedPlanCounts, actualPlanCounts) {
		t.Fatalf("unexpected verified plan counts: got=%#v want=%#v", actualPlanCounts, expectedPlanCounts)
	}

	forbiddenNames := []string{transientName, dotName, tildeName, partName, tmpName, renameBaseName}
	for _, comparison := range report.ScanComparisons {
		for _, forbidden := range forbiddenNames {
			if slicesContains(comparison.GoStableRelativePaths, forbidden) || slicesContains(comparison.PythonStableRelativePaths, forbidden) {
				t.Fatalf("forbidden fixture %q should never become stable: %#v", forbidden, comparison)
			}
		}
	}
	for _, check := range report.PlanChecks {
		for _, forbidden := range forbiddenNames {
			if check.RelativePath == forbidden {
				t.Fatalf("forbidden fixture %q should never reach plan verification", forbidden)
			}
		}
	}

	currentWatch := snapshotSimpleDir(watchRoot, t)
	expectedWatch := map[string]int64{
		filepath.Base(sourceSentinel): sentinelSize,
	}
	if !reflect.DeepEqual(expectedWatch, currentWatch) {
		t.Fatalf("watch root was mutated unexpectedly: got=%#v want=%#v", currentWatch, expectedWatch)
	}
	for key, before := range initialOutputs {
		after := snapshotSimpleDir(map[string]string{
			"audio":      audioRoot,
			"transcript": transcriptRoot,
			"error":      errorRoot,
			"tmp":        tmpRoot,
		}[key], t)
		if !reflect.DeepEqual(before, after) {
			t.Fatalf("%s output path was mutated during acceptance run %d", key, runIndex+1)
		}
	}
	if before := initialDB; !reflect.DeepEqual(before, snapshotFile(dbPath)) {
		t.Fatalf("db path was mutated during acceptance run %d: before=%+v after=%+v", runIndex+1, before, snapshotFile(dbPath))
	}

	encodedReport, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	reportPayload := string(encodedReport)
	for _, leaked := range []string{repoRoot, watchRoot, dbPath} {
		if strings.Contains(reportPayload, leaked) {
			t.Fatalf("acceptance report leaked absolute path %q", leaked)
		}
	}

	return report.VerifiedCount, report
}

func ageDarwinAcceptanceFile(path string) error {
	aged := time.Now().Add(-5 * time.Second)
	return os.Chtimes(path, aged, aged)
}

func writeShadowAcceptanceConfig(t *testing.T, watchFolder, audioRoot, transcriptRoot, errorRoot, tmpRoot, dbPath string) string {
	t.Helper()
	configPath := filepath.Join(t.TempDir(), "shadow_acceptance.yaml")
	configPayload := []byte(
		"app:\n" +
			"  polling_interval_sec: 1\n" +
			"  stable_for_sec: 1\n" +
			"  stale_processing_hours: 1\n" +
			"  transcribe_max_retries: 0\n" +
			"\n" +
			"paths:\n" +
			"  watch_folder: " + watchFolder + "\n" +
			"  stable_audio_folder: " + audioRoot + "\n" +
			"  transcript_folder: " + transcriptRoot + "\n" +
			"  error_folder: " + errorRoot + "\n" +
			"  tmp_dir: " + tmpRoot + "\n" +
			"  db_path: " + dbPath + "\n" +
			"\n" +
			"transcribe:\n" +
			"  model_size: tiny\n" +
			"  device: cpu\n" +
			"  compute_type: int8\n" +
			"  language: ko\n" +
			"  task: transcribe\n" +
			"  beam_size: 1\n" +
			"  vad_filter: false\n" +
			"  word_timestamps: false\n" +
			"\n" +
			"ffmpeg:\n" +
			"  binary_path: /usr/bin/true\n",
	)
	if err := os.WriteFile(configPath, configPayload, 0o600); err != nil {
		t.Fatal(err)
	}
	return configPath
}
