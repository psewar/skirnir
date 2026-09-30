//go:build !windows

package main

import "errors"

// Grafikspeicher je Prozess gibt es vorerst nur unter Windows (Leistungsindikator, gpuproc_windows.go). Unter Linux
// koennte nvidia-smi --query-compute-apps es liefern; bis dahin meldet der Agent dort keinen Kinder-Speicher.
func gpuProcessVRAM() (map[uint32]uint64, error) {
	return nil, errors.New("Grafikspeicher je Prozess nur unter Windows")
}

func (t *procTree) pids() []uint32 { return nil }
