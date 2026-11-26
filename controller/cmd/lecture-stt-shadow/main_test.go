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
	// Release the grandchild's next forks at the cancellation boundary.
	forkGate := filepath.Join(t.TempDir(), "fork-gate")
	if err := syscall.Mkfifo(forkGate, 0o600); err != nil {
		t.Fatal(err)
	}
	gate, err := os.OpenFile(forkGate, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close()
	fakePython := writeSIGTERMFixturePython(t, watchRoot, parentPIDPath, childPIDPath, grandchildPIDPath, forkGate)
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
	detachedPIDPath := filepath.Join(t.TempDir(), "detached.pid")
	if runtime.GOOS == "darwin" {
		command.Env = append(command.Env, "SHADOW_FIXTURE_BINARY="+testBinary, "SHADOW_DETACHED_PID="+detachedPIDPath)
	}
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
	if runtime.GOOS == "darwin" {
		waitForPIDFile(t, detachedPIDPath)
	}

	cleanupStarted := time.Now()
	if _, err := gate.WriteString("fork\n"); err != nil {
		t.Fatal(err)
	}
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

	leader := readPIDFile(t, parentPIDPath)
	known := map[int]bool{
		leader: true, readPIDFile(t, childPIDPath): true, readPIDFile(t, grandchildPIDPath): true,
	}
	if runtime.GOOS == "darwin" {
		known[readPIDFile(t, detachedPIDPath)] = true
	}
	for {
		members := fixtureProcessStates(t, leader, known)
		if !hasLiveFixtureMember(members) {
			// The helper owns and must reap the direct child. Orphaned
			// grandchildren are reaped asynchronously by the system.
			for _, member := range members {
				if member.pid == leader {
					t.Fatalf("direct helper was not reaped: %+v", member)
				}
			}
			if len(members) > 0 {
				t.Logf("exited fixture descendants awaiting OS reaping: %+v", members)
			}
			break
		}
		if time.Since(cleanupStarted) >= 2*time.Second {
			t.Fatalf("live fixture descendants survived cancellation: pgid=%d members=%+v", leader, members)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

type fixtureProcessState struct {
	pid, parent, group int
	state              string
}

func fixtureProcessStates(t *testing.T, group int, known map[int]bool) []fixtureProcessState {
	t.Helper()
	// Use an independent observer. Only report numeric identities and state
	// for this fixture, never other process arguments or environment data.
	output, err := exec.Command("ps", "-ax", "-o", "pid=,ppid=,pgid=,state=").Output()
	if err != nil {
		t.Fatalf("inspect fixture processes: %v", err)
	}
	var result []fixtureProcessState
	for _, line := range strings.Split(strings.TrimSpace(string(output)), "\n") {
		var process fixtureProcessState
		if _, err := fmt.Sscan(line, &process.pid, &process.parent, &process.group, &process.state); err != nil {
			t.Fatalf("could not decode process state: %v", err)
		}
		if process.group == group || known[process.pid] {
			result = append(result, process)
		}
	}
	return result
}

func hasLiveFixtureMember(members []fixtureProcessState) bool {
	for _, member := range members {
		if !strings.HasPrefix(member.state, "Z") {
			return true
		}
	}
	return false
}

func TestExitedButUnreapedFixtureMemberIsNotLive(t *testing.T) {
	// Keep an actual exited process waitable so the zombie observation does
	// not depend on how quickly the host reaps adopted grandchildren.
	cmd := exec.Command("sh", "-c", "exit 0")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer cmd.Wait()
	pid := cmd.Process.Pid
	deadline := time.Now().Add(2 * time.Second)
	for {
		members := fixtureProcessStates(t, pid, nil)
		if len(members) == 1 && members[0].pid == pid && !hasLiveFixtureMember(members) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("could not observe the fixture as a zombie: %+v", members)
		}
		time.Sleep(time.Millisecond)
	}
}

func TestShadowDetachedFixtureProcess(t *testing.T) {
	path := os.Getenv("SHADOW_DETACHED_PID")
	if path == "" {
		return
	}
	if err := syscall.Setpgid(0, 0); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(strconv.Itoa(os.Getpid())), 0o600); err != nil {
		t.Fatal(err)
	}
	time.Sleep(5 * time.Second)
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

func writeSIGTERMFixturePython(t *testing.T, watchRoot, parentPIDPath, childPIDPath, grandchildPIDPath, forkGate string) string {
	t.Helper()
	descendantPath := filepath.Join(t.TempDir(), "descendant-helper")
	descendantScript := `#!/bin/sh
set -eu

mode="$1"
if [ "$mode" = "child" ]; then
  echo $$ > "$2"
  "$0" grandchild "$3" "$4" &
  while :; do sleep 60; done
fi

if [ "$mode" = "grandchild" ]; then
  echo $$ > "$2"
  IFS= read -r _ < "$3"
  for n in 1 2 3 4 5 6 7 8; do sleep 60 & done
  wait
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
    if [ -n "${SHADOW_DETACHED_PID:-}" ]; then
      "$SHADOW_FIXTURE_BINARY" -test.run='^TestShadowDetachedFixtureProcess$' &
    fi
    echo $$ > %q
    %q child %q %q %q &
    while [ ! -s %q ] || [ ! -s %q ]; do
      sleep 0.01
    done
    while :; do sleep 60; done
  fi
fi

exit 2
`, shadow.ConfigSchemaVersion, watchRoot, parentPIDPath, descendantPath, childPIDPath, grandchildPIDPath, forkGate, childPIDPath, grandchildPIDPath)
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
		if value, err := os.ReadFile(path); err == nil {
			if pid, err := strconv.Atoi(strings.TrimSpace(string(value))); err == nil && pid > 1 {
				return
			}
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
