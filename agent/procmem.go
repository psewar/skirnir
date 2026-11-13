package main

// procMem: what a process (or a process tree, summed) holds of the RAM, in bytes (procmem_windows.go, ADR-0092).
type procMem struct {
	Resident uint64 // working set: pages in RAM, private and file-backed
	Commit   uint64 // committed private memory, for comparison only
}
