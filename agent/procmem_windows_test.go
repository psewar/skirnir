//go:build windows

package main

import (
	"os"
	"testing"

	"golang.org/x/sys/windows"
)

// The real call (ADR-0092): memory only committed raises the commit but not the resident measure; touching it raises
// both. A wrong struct size or layout would return false or 0.
func TestProcMemory(t *testing.T) {
	pid := uint32(os.Getpid())
	start, ok := procMemory(pid)
	if !ok || start.Resident < 1<<20 || start.Commit < 1<<20 {
		t.Fatalf("own process: %+v, ok=%v", start, ok)
	}
	const size = 256 << 20
	addr, err := windows.VirtualAlloc(0, size, windows.MEM_RESERVE|windows.MEM_COMMIT, windows.PAGE_READWRITE)
	if err != nil {
		t.Fatal(err)
	}
	defer windows.VirtualFree(addr, 0, windows.MEM_RELEASE)
	committed, ok := procMemory(pid)
	if !ok || committed.Commit < start.Commit+200<<20 {
		t.Fatalf("256 MiB committed: commit %d -> %d", start.Commit, committed.Commit)
	}
	if committed.Resident > start.Resident+64<<20 {
		t.Fatalf("committed but never touched is not resident: %d -> %d", start.Resident, committed.Resident)
	}
	// Touch every page; through the system call, as the address is the system's, not a Go pointer
	windows.NewLazySystemDLL("ntdll.dll").NewProc("RtlFillMemory").Call(addr, size, 1)
	touched, ok := procMemory(pid)
	if !ok || touched.Resident < committed.Resident+200<<20 {
		t.Fatalf("256 MiB touched: resident %d -> %d", committed.Resident, touched.Resident)
	}
	if _, ok := procMemory(0xFFFFFFF0); ok {
		t.Fatal("a process that does not exist: false")
	}
}
