//go:build darwin

package shadow

import (
	"os"
	"time"

	"golang.org/x/sys/unix"
)

const (
	// P_stat values from Darwin sys/proc.h.
	processStopped = 4
	processZombie  = 5
)

func groupHasLeader(members []unix.KinfoProc, pgid int) bool {
	for _, member := range members {
		if int(member.Proc.P_pid) == pgid && int(member.Eproc.Pgid) == pgid {
			return true
		}
	}
	return false
}

// Confirm that the filtered query includes our leader. If it fails or omits
// the leader, independently filter the process table.
func processGroupMembers(pgid int) ([]unix.KinfoProc, bool) {
	members, err := unix.SysctlKinfoProcSlice("kern.proc.pgrp", pgid)
	if err == nil && groupHasLeader(members, pgid) {
		return members, true
	}
	all, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil {
		return nil, false
	}
	members = nil
	for _, process := range all {
		if int(process.Eproc.Pgid) == pgid {
			members = append(members, process)
		}
	}
	return members, groupHasLeader(members, pgid)
}

// Capture the proven subtree while its leader and in-group parents are still
// stopped. Detached descendants must be visited before their parents exit.
func killDetachedDescendants(leader *os.Process, deadline time.Time) {
	all, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil || !groupHasLeader(all, leader.Pid) {
		return
	}
	byPID := make(map[int]unix.KinfoProc, len(all))
	children := make(map[int][]int)
	for _, member := range all {
		pid, parent := int(member.Proc.P_pid), int(member.Eproc.Ppid)
		if pid > 1 && parent > 0 {
			byPID[pid] = member
			children[parent] = append(children[parent], pid)
		}
	}
	// A process can exit or reparent after the snapshot. Verify every parent
	// edge and birth time up to the live leader before signaling a detached PID.
	stillOwned := func(pid int) bool {
		for depth := 0; depth <= len(byPID) && time.Now().Before(deadline); depth++ {
			saved, ok := byPID[pid]
			if !ok {
				return false
			}
			current, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
			if err != nil || current.Proc.P_pid != saved.Proc.P_pid ||
				current.Proc.P_starttime != saved.Proc.P_starttime {
				return false
			}
			if pid == leader.Pid {
				return current.Eproc.Pgid == int32(pid) && leader.Signal(unix.Signal(0)) == nil
			}
			if current.Eproc.Ppid != saved.Eproc.Ppid {
				return false
			}
			pid = int(current.Eproc.Ppid)
		}
		return false
	}
	order := []int{leader.Pid}
	seen := map[int]bool{leader.Pid: true}
	for i := 0; i < len(order); i++ {
		for _, child := range children[order[i]] {
			if !seen[child] {
				seen[child] = true
				order = append(order, child)
			}
		}
	}
	for i := len(order) - 1; i > 0 && time.Now().Before(deadline); i-- {
		pid := order[i]
		member := byPID[pid]
		if int(member.Eproc.Pgid) != leader.Pid && member.Proc.P_stat != processZombie && stillOwned(pid) {
			_ = unix.Kill(pid, unix.SIGKILL)
		}
	}
}

func drainProcessGroup(leader *os.Process) {
	// Keep the leader stopped while late forks finish and group members exit.
	// The final group kill and Wait then reap our direct child. Orphaned zombies
	// have already released their descriptors and await the system reaper.
	pgid := leader.Pid
	deadline := time.Now().Add(250 * time.Millisecond)
	killDetachedDescendants(leader, deadline)
	for time.Now().Before(deadline) && leader.Signal(unix.Signal(0)) == nil {
		members, ok := processGroupMembers(pgid)
		if !ok {
			return
		}
		drained := true
		for _, member := range members {
			if time.Now().After(deadline) {
				return
			}
			pid := int(member.Proc.P_pid)
			if int(member.Eproc.Pgid) != pgid || member.Proc.P_stat == processZombie {
				continue
			}
			if pid == pgid {
				if member.Proc.P_stat != processStopped {
					drained = false
				}
				continue
			}
			drained = false
			// A process may change group or exit between observations. Only
			// signal the same group member with the captured birth time.
			current, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
			if err == nil && current.Proc.P_pid == member.Proc.P_pid &&
				int(current.Eproc.Pgid) == pgid && current.Proc.P_starttime == member.Proc.P_starttime {
				_ = unix.Kill(pid, unix.SIGKILL)
			}
		}
		if drained {
			return
		}
		time.Sleep(time.Millisecond)
	}
}
