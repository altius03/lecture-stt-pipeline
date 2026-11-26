//go:build linux

package shadow

import "syscall"

type fileIdentity struct {
	device    uint64
	inode     uint64
	mode      uint32
	links     uint64
	sizeBytes int64
	mtimeNS   int64
	ctimeNS   int64
}

func identityFromStat(value *syscall.Stat_t) fileIdentity {
	return fileIdentity{
		device:    uint64(value.Dev),
		inode:     uint64(value.Ino),
		mode:      value.Mode,
		links:     uint64(value.Nlink),
		sizeBytes: value.Size,
		mtimeNS:   syscall.TimespecToNsec(value.Mtim),
		ctimeNS:   syscall.TimespecToNsec(value.Ctim),
	}
}
