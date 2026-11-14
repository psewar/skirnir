//go:build !windows

package main

// procMemory: not measured per process outside Windows (the supervisor lists the process tree only there, procTree.pids).
// The agent still learns the upstream's RAM need on Linux from the RAM released when the server stops (upstream_hold.go).
func procMemory(pid uint32) (procMem, bool) { return procMem{}, false }

// systemCacheBytes: not read outside Windows. On Linux, MemAvailable already rises only by the anonymous memory a stopped
// server frees: its file pages stay in the page cache, which MemAvailable counted as reclaimable before and after.
func systemCacheBytes() (uint64, bool) { return 0, false }
