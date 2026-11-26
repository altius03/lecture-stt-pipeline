//go:build darwin || linux

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"lecture-stt/controller/internal/shadow"
)

func TestMainSIGTERMEmitsInterruptedReportAndReapsDescendants(t *testing.T) {
	if runtime.GOOS != "darwin" && runtime.GOOS != "linux" {
		t.Skip("signal integration requires a Unix process model")
	}

	repoRoot := locateCommandRepositoryRoot(t)
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

	parentPIDPath := filepath.Join(t.TempDir(), "fake-python.pid")
	childPIDPath := filepath.Join(t.TempDir(), "fake-python-child.pid")
	grandchildPIDPath := filepath.Join(t.TempDir(), "fake-python-grandchild.pid")
	fakePython := writeSIGTERMFixturePython(t, watchRoot, parentPIDPath, childPIDPath, grandchildPIDPath)
	configPath := writeCommandIntegrationConfig(t, watchRoot, audioRoot, transcriptRoot, errorRoot, tmpRoot, dbPath)

	args := []string{
		"-test.run=TestShadowMainHelperProcess",
		"--",
		"--python-bin", fakePython,
		"--repo-root", repoRoot,
		"--config", configPath,
		"--watch-folder", watchRoot,
		"--scan-count", "1",
		"--sleep-sec", "0",
	}
	testBinary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	command := exec.Command(testBinary, args...)
	command.Env = append(os.Environ(), "GO_WANT_SHADOW_MAIN_HELPER=1")
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr

	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	waitForPIDFile(t, parentPIDPath)
	waitForPIDFile(t, childPIDPath)
	waitForPIDFile(t, grandchildPIDPath)

	cleanupStarted := time.Now()
	if err := command.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	err = command.Wait()
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("expected SIGTERM helper to exit non-zero, got %v", err)
	}
	if exitErr.ExitCode() != 130 {
		t.Fatalf("expected exit code 130 after SIGTERM, got %d (stderr=%s)", exitErr.ExitCode(), stderr.String())
	}
	if elapsed := time.Since(cleanupStarted); elapsed > 2*time.Second {
		t.Fatalf("SIGTERM cleanup exceeded bound: %s", elapsed)
	}

	var report shadow.Report
	if decodeErr := json.Unmarshal(stdout.Bytes(), &report); decodeErr != nil {
		t.Fatalf("stdout did not contain a shadow report: %v\nstdout=%s\nstderr=%s", decodeErr, stdout.String(), stderr.String())
	}
	if report.ErrorKind != "interrupted" {
		t.Fatalf("expected interrupted report, got %#v", report)
	}
	if report.Mode != "read_only" || report.OK {
		t.Fatalf("unexpected command report after SIGTERM: %#v", report)
	}

	for _, pidPath := range []string{parentPIDPath, childPIDPath, grandchildPIDPath} {
		pid := readPIDFile(t, pidPath)
		if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
			t.Fatalf("fixture descendant still exists after SIGTERM cleanup: pid=%d err=%v", pid, err)
		}
	}
}

func TestShadowMainHelperProcess(t *testing.T) {
	if os.Getenv("GO_WANT_SHADOW_MAIN_HELPER") != "1" {
		return
	}
	separator := -1
	for index, value := range os.Args {
		if value == "--" {
			separator = index
			break
		}
	}
	if separator == -1 {
		os.Exit(2)
	}
	os.Args = append([]string{"lecture-stt-shadow"}, os.Args[separator+1:]...)
	flag.CommandLine = flag.NewFlagSet(os.Args[0], flag.ExitOnError)
	main()
	os.Exit(0)
}

func writeSIGTERMFixturePython(t *testing.T, watchRoot, parentPIDPath, childPIDPath, grandchildPIDPath string) string {
	t.Helper()
	descendantPath := filepath.Join(t.TempDir(), "descendant-helper")
	descendantScript := `#!/bin/sh
set -eu

mode="$1"
if [ "$mode" = "child" ]; then
  echo $$ > "$2"
  "$0" grandchild "$3" &
  while :; do sleep 60; done
fi

if [ "$mode" = "grandchild" ]; then
  echo $$ > "$2"
  while :; do sleep 60; done
fi

exit 2
`
	if err := os.WriteFile(descendantPath, []byte(descendantScript), 0o700); err != nil {
		t.Fatal(err)
	}
	scriptPath := filepath.Join(t.TempDir(), "fake-python")
	script := fmt.Sprintf(`#!/bin/sh
set -eu

if [ "$1" != "-m" ]; then
  exit 2
fi

if [ "$2" = "lecture_stt.stt.shadow_probe" ]; then
  if [ "$3" = "config" ]; then
    cat <<'JSON'
{"schema_version":"%s","watch_folder":"%s","stable_for_sec":0,"polling_interval_sec":1}
JSON
    exit 0
  fi
  if [ "$3" = "lockstep-scan" ]; then
    echo $$ > %q
    %q child %q %q &
    while [ ! -s %q ] || [ ! -s %q ]; do
      sleep 0.01
    done
    while :; do sleep 60; done
  fi
fi

exit 2
`, shadow.ConfigSchemaVersion, watchRoot, parentPIDPath, descendantPath, childPIDPath, grandchildPIDPath, childPIDPath, grandchildPIDPath)
	if err := os.WriteFile(scriptPath, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	return scriptPath
}

func writeCommandIntegrationConfig(t *testing.T, watchFolder, audioRoot, transcriptRoot, errorRoot, tmpRoot, dbPath string) string {
	t.Helper()
	configPath := filepath.Join(t.TempDir(), "shadow_command.yaml")
	configPayload := []byte(
		"app:\n" +
			"  polling_interval_sec: 1\n" +
			"  stable_for_sec: 0\n" +
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

func waitForPIDFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		if _, err := os.Stat(path); err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("pid file was not published: %s", path)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func readPIDFile(t *testing.T, path string) int {
	t.Helper()
	value, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(value)))
	if err != nil {
		t.Fatal(err)
	}
	return pid
}

func locateCommandRepositoryRoot(t *testing.T) string {
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
