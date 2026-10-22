//go:build windows

package main

import (
	"golang.org/x/sys/windows"
)

// diskFree: free space for the calling user on the volume of path (GetDiskFreeSpaceEx).
func diskFree(path string) (uint64, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	var free, total, totalFree uint64
	if err := windows.GetDiskFreeSpaceEx(p, &free, &total, &totalFree); err != nil {
		return 0, err
	}
	return free, nil
}
