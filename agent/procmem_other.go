//go:build !windows

package main

// procMemory: not measured per process outside Windows (the supervisor lists the process tree only there, procTree.pids).
// The agent still learns the upstream's RAM need on Linux from the RAM released when the server stops (upstream_hold.go).
func procMemory(pid uint32) (procMem, bool) { return procMem{}, false }
