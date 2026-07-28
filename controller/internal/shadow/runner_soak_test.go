package shadow

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestRunWithScanHookSoak_RealPythonProbe(t *testing.T) {
	if os.Getenv("LECTURE_STT_RUN_SHADOW_SOAK") != "1" {
		t.Skip("set LECTURE_STT_RUN_SHADOW_SOAK=1 to run this integration soak test")
	}

	repoRoot := locateRepositoryRoot(t)
	pythonPath := filepath.Join(repoRoot, ".venv", "bin", "python")
	pythonInfo, err := os.Stat(pythonPath)
	if err != nil || pythonInfo.IsDir() || pythonInfo.Mode()&0o111 == 0 {
		t.Skip("project .venv/python is required for this integration soak test")
	}

	watchRoot := t.TempDir()
	audioRoot := filepath.Join(t.TempDir(), "audio")
	transcriptRoot := filepath.Join(t.TempDir(), "transcripts")
	errorRoot := filepath.Join(t.TempDir(), "errors")
	tmpRoot := filepath.Join(t.TempDir(), "tmp")
	dbPath := filepath.Join(t.TempDir(), "state", "jobs.sqlite3")

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

	metadata := map[string]map[string]int64{
		"audio":      snapshotSimpleDir(audioRoot, t),
		"transcript": snapshotSimpleDir(transcriptRoot, t),
		"error":      snapshotSimpleDir(errorRoot, t),
		"tmp":        snapshotSimpleDir(tmpRoot, t),
	}
	dbMeta := snapshotFile(dbPath)

	stableRelativePath := "안정된_클래스_파일.m4a"
	renamedRelativePath := "안정된_클래스_파일-renamed.m4a"
	unicodeRelativePath := "문자열_🗂️.m4a"

	stableSource := filepath.Join(watchRoot, stableRelativePath)
	renamedSource := filepath.Join(watchRoot, renamedRelativePath)
	unicodeSource := filepath.Join(watchRoot, unicodeRelativePath)
	transientPath := filepath.Join(watchRoot, "transient.m4a")
	tempPath := filepath.Join(watchRoot, ".shadow-upload.part")
	sourceSentinel := filepath.Join(watchRoot, ".watch-sentinel")

	stablePayload := []byte("stable-audio")
	unicodePayload := []byte("unicode-audio")
	if err := os.WriteFile(sourceSentinel, []byte("stable-root-sentinel"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(stableSource, stablePayload, 0o600); err != nil {
		t.Fatal(err)
	}
	initialWatchState := snapshotSimpleDir(watchRoot, t)
	sourceSentinelSize := initialWatchState[filepath.Base(sourceSentinel)]
	stableSourceSHA := sha256HexString(stablePayload)
	unicodeSourceSHA := sha256HexString(unicodePayload)

	options := Options{
		PythonBin:           pythonPath,
		RepoRoot:            repoRoot,
		ConfigPath:          writeShadowIntegrationConfig(t, watchRoot, audioRoot, transcriptRoot, errorRoot, tmpRoot, dbPath),
		WatchFolderOverride: watchRoot,
		ScanCount:           7,
	}
	sleep := 1.0
	options.SleepSec = &sleep

	report, err := runWithScanHook(options, func(scanIndex int, _ string) error {
		switch scanIndex {
		case 1:
			return os.WriteFile(transientPath, []byte("in-flight-1"), 0o600)
		case 2:
			if f, openErr := os.OpenFile(transientPath, os.O_APPEND|os.O_WRONLY, 0o600); openErr == nil {
				if _, writeErr := f.WriteString("append"); writeErr != nil {
					_ = f.Close()
					return writeErr
				}
				if closeErr := f.Close(); closeErr != nil {
					return closeErr
				}
			} else {
				return openErr
			}
			if err := os.WriteFile(tempPath, []byte("still-temp"), 0o600); err != nil {
				return err
			}
			if err := os.Remove(transientPath); err != nil {
				return err
			}
			return nil
		case 3:
			if err := os.Rename(stableSource, renamedSource); err != nil {
				return err
			}
			if err := os.WriteFile(transientPath, []byte("never-stable"), 0o600); err != nil {
				return err
			}
			if err := os.Remove(tempPath); err != nil && !os.IsNotExist(err) {
				return err
			}
			if err := os.Remove(transientPath); err != nil {
				return err
			}
			return nil
		case 5:
			if err := os.Remove(renamedSource); err != nil {
				return err
			}
			if err := os.WriteFile(unicodeSource, unicodePayload, 0o600); err != nil {
				return err
			}
			return nil
		case 6:
			return nil
		case 7:
			if err := os.Remove(unicodeSource); err != nil {
				return err
			}
			return nil
		default:
			return nil
		}
	})
	if err != nil {
		t.Fatalf("runWithScanHook() failed: %v", err)
	}

	if report.ErrorKind != "" {
		t.Fatalf("expected successful shadow run, got error_kind=%q", report.ErrorKind)
	}
	if !report.PollingMatch {
		t.Fatal("expected polling match when Go and Python paths are aligned")
	}
	if !report.OK {
		t.Fatalf("expected runWithScanHook to be OK: %#v", report)
	}
	if len(report.PlanChecks) != 3 {
		t.Fatalf("expected 3 plan checks, got %d", len(report.PlanChecks))
	}
	for i, check := range []struct {
		RelativePath string
		ScanIndex    int
	}{
		{RelativePath: stableRelativePath, ScanIndex: 2},
		{RelativePath: renamedRelativePath, ScanIndex: 4},
		{RelativePath: unicodeRelativePath, ScanIndex: 6},
	} {
		if got := report.PlanChecks[i].RelativePath; got != check.RelativePath {
			t.Fatalf("expected plan check path %q, got %q", check.RelativePath, got)
		}
		if got := report.PlanChecks[i].ScanIndex; got != check.ScanIndex {
			t.Fatalf("expected plan check scan %d, got %d", check.ScanIndex, got)
		}
		if report.PlanChecks[i].Status != "verified" {
			t.Fatalf("expected verified plan check for %q, got %+v", check.RelativePath, report.PlanChecks[i])
		}
	}
	if report.VerifiedCount != 3 {
		t.Fatalf("expected 3 verified plan checks, got %d", report.VerifiedCount)
	}
	if len(report.ScanComparisons) != 7 {
		t.Fatalf("expected 7 scan comparisons, got %d", len(report.ScanComparisons))
	}
	for index, comparison := range report.ScanComparisons {
		expectedPath := ""
		switch comparison.ScanIndex {
		case 2:
			expectedPath = stableRelativePath
		case 4:
			expectedPath = renamedRelativePath
		case 6:
			expectedPath = unicodeRelativePath
		}
		if !comparison.Match {
			t.Fatalf("expected matching scan comparison at slot %d", index+1)
		}
		if comparison.ScanIndex != index+1 {
			t.Fatalf("expected scan_index=%d, got %d", index+1, comparison.ScanIndex)
		}
		if len(comparison.GoStableRelativePaths) > 0 {
			for _, name := range comparison.GoStableRelativePaths {
				if strings.Contains(name, "/") || strings.Contains(name, "\\") {
					t.Fatalf("expected relative path in Go scan comparison, got %q", name)
				}
				if name == ".shadow-upload.part" {
					t.Fatalf("temporary file should not be stable: %q", name)
				}
				if name == "transient.m4a" {
					t.Fatalf("transient file should not be stable: %q", name)
				}
			}
		}
		if expectedPath != "" && !slicesContains(comparison.GoStableRelativePaths, expectedPath) {
			t.Fatalf("expected %q on scan %d, got %#v", expectedPath, comparison.ScanIndex, comparison.GoStableRelativePaths)
		}
	}

	currentWatchState := snapshotSimpleDir(watchRoot, t)
	expectedWatchState := map[string]int64{
		filepath.Base(sourceSentinel): sourceSentinelSize,
	}
	if !reflect.DeepEqual(expectedWatchState, currentWatchState) {
		t.Fatalf("watch root was mutated unexpectedly: %#v", currentWatchState)
	}

	if report.VerifiedCount != 3 {
		t.Fatalf("expected 3 verified plan checks, got %d", report.VerifiedCount)
	}
	currentAudio := snapshotSimpleDir(audioRoot, t)
	currentTranscript := snapshotSimpleDir(transcriptRoot, t)
	currentError := snapshotSimpleDir(errorRoot, t)
	currentTmp := snapshotSimpleDir(tmpRoot, t)
	if !reflect.DeepEqual(metadata["audio"], currentAudio) {
		t.Fatalf("audio output path was mutated during soak run")
	}
	if !reflect.DeepEqual(metadata["transcript"], currentTranscript) {
		t.Fatalf("transcript output path was mutated during soak run")
	}
	if !reflect.DeepEqual(metadata["error"], currentError) {
		t.Fatalf("error output path was mutated during soak run")
	}
	if !reflect.DeepEqual(metadata["tmp"], currentTmp) {
		t.Fatalf("tmp output path was mutated during soak run")
	}
	if before := dbMeta; !reflect.DeepEqual(before, snapshotFile(dbPath)) {
		t.Fatalf("db path was mutated during soak run: before=%+v after=%+v", before, snapshotFile(dbPath))
	}

	encodedReport, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	reportPayload := string(encodedReport)
	if strings.Contains(reportPayload, repoRoot) {
		t.Fatalf("report leaked absolute repo root path")
	}
	if strings.Contains(reportPayload, watchRoot) {
		t.Fatalf("report leaked absolute watch folder path")
	}
	if strings.Contains(reportPayload, dbPath) {
		t.Fatalf("report leaked absolute database path")
	}
	if strings.Contains(reportPayload, stableSourceSHA) {
		t.Fatalf("report leaked stable source SHA")
	}
	if strings.Contains(reportPayload, unicodeSourceSHA) {
		t.Fatalf("report leaked unicode source SHA")
	}
}

func writeShadowIntegrationConfig(t *testing.T, watchFolder, audioRoot, transcriptRoot, errorRoot, tmpRoot, dbPath string) string {
	t.Helper()
	configPath := filepath.Join(t.TempDir(), "shadow_config.yaml")
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

func snapshotSimpleDir(path string, t *testing.T) map[string]int64 {
	t.Helper()
	entries, err := os.ReadDir(path)
	if err != nil {
		t.Fatal(err)
	}
	state := make(map[string]int64, len(entries))
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil {
			t.Fatal(err)
		}
		state[entry.Name()] = info.Size()
	}
	return state
}

func snapshotFile(path string) struct {
	Exists bool
	Size   int64
	Mode   os.FileMode
} {
	info, err := os.Stat(path)
	if err != nil {
		return struct {
			Exists bool
			Size   int64
			Mode   os.FileMode
		}{Exists: false}
	}
	return struct {
		Exists bool
		Size   int64
		Mode   os.FileMode
	}{Exists: true, Size: info.Size(), Mode: info.Mode()}
}

func locateRepositoryRoot(t *testing.T) string {
	t.Helper()
	current, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 10; i++ {
		if _, err := os.Stat(filepath.Join(current, "src", "lecture_stt")); err == nil {
			return current
		}
		next := filepath.Dir(current)
		if next == current {
			t.Fatal("could not locate repository root")
		}
		current = next
	}
	t.Fatal("could not locate repository root within 10 directories")
	return ""
}

func slicesContains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func sha256HexString(payload []byte) string {
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}
