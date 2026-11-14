//go:build windows

package main

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	procK32GetProcessMemoryInfo = windows.NewLazySystemDLL("kernel32.dll").NewProc("K32GetProcessMemoryInfo")
	procK32GetPerformanceInfo   = windows.NewLazySystemDLL("kernel32.dll").NewProc("K32GetPerformanceInfo")
)

// PROCESS_MEMORY_COUNTERS_EX2: PROCESS_MEMORY_COUNTERS_EX plus the private working set (Windows 10 and later; an older
// system rejects the larger size, then the EX part is read alone).
type processMemoryCountersEx2 struct {
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
	PrivateWorkingSetSize      uintptr
	SharedCommitUsage          uint64
}

// sizeofCountersEx: the size of PROCESS_MEMORY_COUNTERS_EX, the struct above without its last two fields.
const sizeofCountersEx = unsafe.Offsetof(processMemoryCountersEx2{}.PrivateWorkingSetSize)

// procMemory: what a process holds of the RAM (ADR-0092). Private is its private working set: the private pages in RAM,
// which the system can take back only by paging them out - what a start really takes (agent 0.35.1). Resident is the
// whole working set, which also holds file-backed pages such as the parts of an expert file a server read; with a
// resident budget those were 15 GB of a 29 GB working set, and the system moves them to the standby list (available RAM)
// under pressure, so they are not part of the need. Commit counts reserved memory that never becomes resident (52 GiB
// against that 29 GB). Resident and Commit are kept for comparison. Pages a driver holds for the process outside its
// working set (pinned for the GPU and trimmed, or allocated by the graphics kernel) show in none of them; the agent sees
// them in the RAM released when the server stops (upstream_hold.go). false = the process is gone or not readable.
func procMemory(pid uint32) (procMem, bool) {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil { // the process may end between listing and opening (a race no check avoids); nothing to count then
		return procMem{}, false
	}
	defer windows.CloseHandle(h)
	var c processMemoryCountersEx2
	c.CB = uint32(unsafe.Sizeof(c))
	if r, _, _ := procK32GetProcessMemoryInfo.Call(uintptr(h), uintptr(unsafe.Pointer(&c)), uintptr(c.CB)); r != 0 {
		return procMem{Private: uint64(c.PrivateWorkingSetSize), Resident: uint64(c.WorkingSetSize), Commit: uint64(c.PrivateUsage)}, true
	}
	// A system without the EX2 counters: the whole working set stands in for the private one (on the safe side)
	c = processMemoryCountersEx2{CB: uint32(sizeofCountersEx)}
	if r, _, _ := procK32GetProcessMemoryInfo.Call(uintptr(h), uintptr(unsafe.Pointer(&c)), uintptr(c.CB)); r == 0 {
		return procMem{}, false
	}
	return procMem{Private: uint64(c.WorkingSetSize), Resident: uint64(c.WorkingSetSize), Commit: uint64(c.PrivateUsage)}, true
}

// PERFORMANCE_INFORMATION
type performanceInformation struct {
	CB                uint32
	CommitTotal       uintptr
	CommitLimit       uintptr
	CommitPeak        uintptr
	PhysicalTotal     uintptr
	PhysicalAvailable uintptr
	SystemCache       uintptr
	KernelTotal       uintptr
	KernelPaged       uintptr
	KernelNonpaged    uintptr
	PageSize          uintptr
	HandleCount       uint32
	ProcessCount      uint32
	ThreadCount       uint32
}

// systemCacheBytes: the system cache, i.e. the standby list plus the system working set (GetPerformanceInfo). When the
// server stops, the file pages it had read go to the standby list: available, but not memory the start took for good.
// The release measure subtracts their growth (upstream_hold.go releasedSince). false = not readable.
func systemCacheBytes() (uint64, bool) {
	var p performanceInformation
	p.CB = uint32(unsafe.Sizeof(p))
	if r, _, _ := procK32GetPerformanceInfo.Call(uintptr(unsafe.Pointer(&p)), uintptr(p.CB)); r == 0 {
		return 0, false
	}
	return uint64(p.SystemCache) * uint64(p.PageSize), true
}
