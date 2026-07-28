//go:build linux

package shadow

// Linux children inherit the explicitly created process group. The group kill
// remains the closed cleanup boundary on that platform.
func descendantPIDs(_ int) []int {
	return nil
}
