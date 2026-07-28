package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAcquireControllerLockRejectsSymlinkWithoutChangingTarget(t *testing.T) {
	stateDir := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(stateDir, 0o700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "target")
	if err := os.WriteFile(target, []byte("do not touch"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(stateDir, "controller.lock")); err != nil {
		t.Fatal(err)
	}

	if file, err := acquireControllerLock(stateDir); err == nil {
		releaseControllerLock(file)
		t.Fatal("expected symlink controller lock to be rejected")
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("lock target mode changed: %o", info.Mode().Perm())
	}
	value, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(value) != "do not touch" {
		t.Fatalf("lock target content changed: %q", value)
	}
}

func TestAcquireControllerLockRejectsPermissiveExistingLock(t *testing.T) {
	stateDir := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(stateDir, 0o700); err != nil {
		t.Fatal(err)
	}
	lockPath := filepath.Join(stateDir, "controller.lock")
	if err := os.WriteFile(lockPath, nil, 0o644); err != nil {
		t.Fatal(err)
	}

	if file, err := acquireControllerLock(stateDir); err == nil {
		releaseControllerLock(file)
		t.Fatal("expected permissive controller lock to be rejected")
	}
	info, err := os.Stat(lockPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("existing lock mode changed: %o", info.Mode().Perm())
	}
}

func TestAcquireControllerLockRejectsContention(t *testing.T) {
	stateDir := filepath.Join(t.TempDir(), "state")
	if err := os.Mkdir(stateDir, 0o700); err != nil {
		t.Fatal(err)
	}

	first, err := acquireControllerLock(stateDir)
	if err != nil {
		t.Fatalf("expected to acquire initial controller lock: %v", err)
	}
	defer releaseControllerLock(first)

	if second, err := acquireControllerLock(stateDir); err == nil {
		releaseControllerLock(second)
		t.Fatal("expected second acquire attempt to fail due to flock contention")
	}
	lockPath := filepath.Join(stateDir, "controller.lock")
	info, err := os.Stat(lockPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("lock mode changed under contention: %o", info.Mode().Perm())
	}
}
