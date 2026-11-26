//go:build darwin || linux

package shadow

import (
	"os/exec"
	"syscall"
	"time"
)

func configureSubprocess(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// A helper can exit after passing an output pipe to another process.
	// Bound os/exec's copy goroutines even if that process is outside our group.
	command.WaitDelay = 500 * time.Millisecond
}

func terminateSubprocess(command *exec.Cmd) {
	if command == nil || command.Process == nil {
		return
	}
	leader := command.Process
	if err := leader.Signal(syscall.SIGSTOP); err != nil {
		return // Wait may already have reaped this leader.
	}
	pid := leader.Pid
	if group, err := syscall.Getpgid(pid); err == nil && group == pid && leader.Signal(syscall.Signal(0)) == nil {
		_ = syscall.Kill(-pid, syscall.SIGSTOP)
		drainProcessGroup(leader)
		// The stopped leader anchors the PGID until the final group signal.
		if leader.Signal(syscall.Signal(0)) == nil {
			_ = syscall.Kill(-pid, syscall.SIGKILL)
		}
	}
	_ = leader.Kill()
}
