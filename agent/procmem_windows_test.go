//go:build windows

package main

import (
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/windows"
)

var ntdllTest = windows.NewLazySystemDLL("ntdll.dll")

// The real call (ADR-0092): memory only committed raises the commit but neither working set; touching it raises the
// private working set and the working set. A wrong struct size or layout would return false or 0.
func TestProcMemory(t *testing.T) {
	pid := uint32(os.Getpid())
	start, ok := procMemory(pid)
	if !ok || start.Private < 1<<20 || start.Resident < start.Private || start.Commit < 1<<20 {
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
	if committed.Private > start.Private+64<<20 || committed.Resident > start.Resident+64<<20 {
		t.Fatalf("committed but never touched is not in the working set: %+v -> %+v", start, committed)
	}
	// Touch every page; through the system call, as the address is the system's, not a Go pointer
	ntdllTest.NewProc("RtlFillMemory").Call(addr, size, 1)
	touched, ok := procMemory(pid)
	if !ok || touched.Private < committed.Private+200<<20 || touched.Resident < committed.Resident+200<<20 {
		t.Fatalf("256 MiB touched: private and whole working set rise: %+v -> %+v", committed, touched)
	}
	if _, ok := procMemory(0xFFFFFFF0); ok {
		t.Fatal("a process that does not exist: false")
	}
}

// ADR-0092, agent 0.35.1: pages of a mapped file the process read (like an expert file under a resident budget) are in
// its working set but not in its private working set - the system takes them back under pressure.
func TestProcMemoryMappedFile(t *testing.T) {
	const size = 128 << 20
	path := filepath.Join(t.TempDir(), "experts.bin")
	if err := os.WriteFile(path, make([]byte, size), 0o600); err != nil {
		t.Fatal(err)
	}
	p16, err := windows.UTF16PtrFromString(path)
	if err != nil {
		t.Fatal(err)
	}
	f, err := windows.CreateFile(p16, windows.GENERIC_READ, windows.FILE_SHARE_READ, nil, windows.OPEN_EXISTING, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer windows.CloseHandle(f)
	mapping, err := windows.CreateFileMapping(f, nil, windows.PAGE_READONLY, 0, 0, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer windows.CloseHandle(mapping)
	view, err := windows.MapViewOfFile(mapping, windows.FILE_MAP_READ, 0, 0, size)
	if err != nil {
		t.Fatal(err)
	}
	defer windows.UnmapViewOfFile(view)

	pid := uint32(os.Getpid())
	before, ok := procMemory(pid)
	if !ok {
		t.Fatal("own process not readable")
	}
	ntdllTest.NewProc("RtlComputeCrc32").Call(0, view, size) // reads every page of the view
	after, ok := procMemory(pid)
	if !ok {
		t.Fatal("own process not readable")
	}
	if after.Resident < before.Resident+100<<20 {
		t.Fatalf("read file pages are in the working set: %d -> %d", before.Resident, after.Resident)
	}
	if after.Private > before.Private+32<<20 {
		t.Fatalf("read file pages are not private: %d -> %d", before.Private, after.Private)
	}
}

// The system cache is readable and plausible (standby list plus system working set, at least some MiB on any system).
func TestSystemCacheBytes(t *testing.T) {
	c, ok := systemCacheBytes()
	if !ok || c < 1<<20 {
		t.Fatalf("system cache: %d bytes, ok=%v", c, ok)
	}
}
