//go:build windows

package main

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

var procK32GetProcessMemoryInfo = windows.NewLazySystemDLL("kernel32.dll").NewProc("K32GetProcessMemoryInfo")

// PROCESS_MEMORY_COUNTERS_EX
type processMemoryCountersEx struct {
	CB                         uint32
	PageFaultCount             uint32
	PeakWorkingSetSize         uintptr
	WorkingSetSize             uintptr
	QuotaPeakPagedPoolUsage    uintptr
	QuotaPagedPoolUsage        uintptr
	QuotaPeakNonPagedPoolUsage uintptr
	QuotaNonPagedPoolUsage     uintptr
	PagefileUsage              uintptr
	PeakPagefileUsage          uintptr
	PrivateUsage               uintptr
}

// procPrivateBytes: committed private memory of a process (what it will occupy in RAM once touched). The working set
// says less: Strata commits its experts at load (~42 GB) and pages them in over hours, partly locked for the GPU
// outside the working set. false = the process is gone or not readable.
func procPrivateBytes(pid uint32) (uint64, bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil { // the process may end between listing and opening (a race no check avoids); nothing to count then
		return 0, false
	}
	defer windows.CloseHandle(h)
	var c processMemoryCountersEx
	c.CB = uint32(unsafe.Sizeof(c))
	if r, _, _ := procK32GetProcessMemoryInfo.Call(uintptr(h), uintptr(unsafe.Pointer(&c)), uintptr(c.CB)); r == 0 {
		return 0, false
	}
	return uint64(c.PrivateUsage), true
}
