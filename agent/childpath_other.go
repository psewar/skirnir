//go:build !windows

package main

import (
	"fmt"
	"os"
	"syscall"
)

// pathExposure (Linux and other Unix): who besides root may change path in the way kind names, from owner and mode.
// A sticky folder above protects entries of other owners from renaming. ACLs are not read; they could only add someone.
func pathExposure(path string, kind exposureKind) (string, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return "", fmt.Errorf("no owner information")
	}
	mode := uint32(fi.Mode().Perm())
	sticky := fi.Mode()&os.ModeSticky != 0
	if st.Uid != 0 {
		return fmt.Sprintf("uid %d (owner)", st.Uid), nil
	}
	if kind == exposeAncestor && sticky {
		return "", nil // others may add entries but not rename or remove root's
	}
	if mode&0o020 != 0 && st.Gid != 0 {
		return fmt.Sprintf("group %d", st.Gid), nil
	}
	if mode&0o002 != 0 {
		return "everyone", nil
	}
	return "", nil
}
