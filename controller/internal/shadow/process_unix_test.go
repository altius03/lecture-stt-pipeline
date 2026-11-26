//go:build darwin || linux

package shadow

import (
	"errors"
	"io"
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestExitedHelperWithInheritedOutputDoesNotHoldWait(t *testing.T) {
	// Keep the child alive on a fixture-owned pipe after the helper exits.
	// Closing release also cleans up the child on any test failure.
	reader, release, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	defer release.Close()
	cmd := exec.Command("sh", "-c", "(read ignored <&3) & exit 0")
	cmd.ExtraFiles = []*os.File{reader}
	cmd.Stderr = io.Discard
	configureSubprocess(cmd)
	fallback := time.AfterFunc(3*time.Second, func() { _ = release.Close() })
	defer fallback.Stop()
	start := time.Now()
	if err := cmd.Run(); !errors.Is(err, exec.ErrWaitDelay) {
		t.Fatalf("expected bounded wait for inherited pipe, got %v", err)
	}
	if elapsed := time.Since(start); elapsed >= 2*time.Second {
		t.Fatalf("inherited pipe held Wait for %v", elapsed)
	}
}
