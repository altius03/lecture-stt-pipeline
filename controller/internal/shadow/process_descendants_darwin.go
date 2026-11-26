//go:build darwin

package shadow

import (
	"os"
	"time"

	"golang.org/x/sys/unix"
)

const maxDescendantProcesses = 4096

// Darwin can fork a child into a process group during a group signal. Wait
// for the stopped group to reach a state where no member can still fork.
func quiesceProcessGroup(leader *os.Process) {
	pgid := leader.Pid
	// Process states from Darwin sys/proc.h.
	const stopped, zombie = 4, 5
	deadline := time.Now().Add(200 * time.Millisecond)
	for time.Now().Before(deadline) {
		members, err := unix.SysctlKinfoProcSlice("kern.proc.pgrp", pgid)
		if err != nil || len(members) == 0 {
			return
		}
		quiescent := true
		for _, member := range members {
			if state := member.Proc.P_stat; state != stopped && state != zombie {
				quiescent = false
				break
			}
		}
		if quiescent || leader.Signal(unix.Signal(0)) != nil {
			return
		}
		// Catch a new member that missed the first stop while forking.
		_ = unix.Kill(-pgid, unix.SIGSTOP)
		time.Sleep(time.Millisecond)
	}
}

func killSubprocessDescendants(rootPID int) {
	processes, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil {
		return
	}
	children := make(map[int][]unix.KinfoProc)
	for _, process := range processes {
		pid := int(process.Proc.P_pid)
		parentPID := int(process.Eproc.Ppid)
		if pid <= 1 || parentPID <= 0 || pid == rootPID {
			continue
		}
		children[parentPID] = append(children[parentPID], process)
	}

	result := make([]unix.KinfoProc, 0)
	seen := map[int]struct{}{rootPID: {}}
	var collect func(int)
	collect = func(parentPID int) {
		if len(result) >= maxDescendantProcesses {
			return
		}
		for _, child := range children[parentPID] {
			childPID := int(child.Proc.P_pid)
			if _, exists := seen[childPID]; exists {
				continue
			}
			seen[childPID] = struct{}{}
			collect(childPID)
			if len(result) >= maxDescendantProcesses {
				return
			}
			result = append(result, child)
		}
	}
	collect(rootPID)
	for _, child := range result {
		if int(child.Eproc.Pgid) == rootPID {
			continue // The entire stopped group receives the final kill.
		}
		pid := int(child.Proc.P_pid)
		current, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
		if err == nil && current.Proc.P_pid == child.Proc.P_pid && current.Proc.P_starttime == child.Proc.P_starttime {
			_ = unix.Kill(pid, unix.SIGKILL)
		}
	}
}
