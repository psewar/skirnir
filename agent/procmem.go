package main

// procMem: what a process (or a process tree, summed) holds of the RAM, in bytes (procmem_windows.go, ADR-0092).
type procMem struct {
	// Private: the private working set - private pages in RAM. The system can take them back only by paging them out, so
	// this is what a start really takes (agent 0.35.1). Pinned pages the process locked in its working set count here.
	Private uint64
	// Resident: the whole working set, including file-backed pages such as the parts of a model file the server read.
	// The system trims those to the standby list (available RAM) under pressure; for comparison only.
	Resident uint64
	Commit   uint64 // committed private memory, for comparison only
}
