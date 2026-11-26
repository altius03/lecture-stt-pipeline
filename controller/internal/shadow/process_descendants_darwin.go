//go:build darwin

package shadow

import "golang.org/x/sys/unix"

const maxDescendantProcesses = 4096

func descendantPIDs(rootPID int) []int {
	processes, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil {
		return nil
	}
	children := make(map[int][]int)
	for _, process := range processes {
		pid := int(process.Proc.P_pid)
		parentPID := int(process.Eproc.Ppid)
		if pid <= 1 || parentPID <= 0 || pid == rootPID {
			continue
		}
		children[parentPID] = append(children[parentPID], pid)
	}

	result := make([]int, 0)
	seen := map[int]struct{}{rootPID: {}}
	var collect func(int)
	collect = func(parentPID int) {
		if len(result) >= maxDescendantProcesses {
			return
		}
		for _, childPID := range children[parentPID] {
			if _, exists := seen[childPID]; exists {
				continue
			}
			seen[childPID] = struct{}{}
			collect(childPID)
			if len(result) >= maxDescendantProcesses {
				return
			}
			result = append(result, childPID)
		}
	}
	collect(rootPID)
	return result
}
