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

// procMemory: what a process holds of the RAM (agent 0.35.0, ADR-0092). Resident is its working set: every page mapped
// into it and present in RAM, private or file-backed (resident pages of mapped model files are not available to others
// either, so the private working set alone would miss them). Commit is its committed private memory, kept only for
// comparison: it also counts what is reserved but not resident (CUDA host allocations, views of expert files), so as the
// need it overstated the RAM a start takes - 52 GiB committed against a 28 GB working set for a server with a 16 GiB
// resident budget. Pages a driver holds for the process outside its working set (pinned for the GPU and trimmed, or
// allocated by the graphics kernel) show in neither; the agent sees them in the RAM released when the server stops
// (upstream_hold.go). false = the process is gone or not readable.
func procMemory(pid uint32) (procMem, bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil { // the process may end between listing and opening (a race no check avoids); nothing to count then
		return procMem{}, false
	}
	defer windows.CloseHandle(h)
	var c processMemoryCountersEx
	c.CB = uint32(unsafe.Sizeof(c))
	if r, _, _ := procK32GetProcessMemoryInfo.Call(uintptr(h), uintptr(unsafe.Pointer(&c)), uintptr(c.CB)); r == 0 {
		return procMem{}, false
	}
	return procMem{Resident: uint64(c.WorkingSetSize), Commit: uint64(c.PrivateUsage)}, true
}
