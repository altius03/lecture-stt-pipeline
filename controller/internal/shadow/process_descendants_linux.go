//go:build linux

package shadow

import "os"

// Linux children inherit the explicitly created process group. The group kill
// remains the closed cleanup boundary on that platform.
func drainProcessGroup(_ *os.Process) {}
