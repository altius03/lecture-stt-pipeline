//go:build darwin || linux

package shadow

import (
	"os/exec"
	"syscall"
)

func configureSubprocess(command *exec.Cmd) {
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func terminateSubprocess(command *exec.Cmd) {
	if command == nil || command.Process == nil {
		return
	}
	pid := command.Process.Pid
	descendants := descendantPIDs(pid)
	_ = syscall.Kill(-pid, syscall.SIGKILL)
	for _, descendant := range descendants {
		_ = syscall.Kill(descendant, syscall.SIGKILL)
	}
	_ = command.Process.Kill()
}
