//go:build !windows

package main

import "errors"

// Per-process GPU memory is currently only available on Windows (performance counter, gpuproc_windows.go). On Linux,
// nvidia-smi --query-compute-apps could provide it; until then, the agent reports no child memory there.
func gpuProcessVRAM() (map[uint32]uint64, error) {
	return nil, errors.New("graphics memory per process only on Windows")
}

func (t *procTree) pids() []uint32 { return nil }

// members: outside Windows the agent does not list a child's process tree (a process group, platform_other.go), so the
// runners of an Ollama child are not counted there (ADR-0148).
func (t *procTree) members() ([]uint32, bool) { return nil, false }

// isConsoleHost: there is no console host process outside Windows.
func isConsoleHost(pid uint32) bool { return false }
