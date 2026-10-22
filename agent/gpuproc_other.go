//go:build !windows

package main

import "errors"

// Per-process GPU memory is currently only available on Windows (performance counter, gpuproc_windows.go). On Linux,
// nvidia-smi --query-compute-apps could provide it; until then, the agent reports no child memory there.
func gpuProcessVRAM() (map[uint32]uint64, error) {
	return nil, errors.New("graphics memory per process only on Windows")
}

func (t *procTree) pids() []uint32 { return nil }
