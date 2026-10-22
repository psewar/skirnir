//go:build !windows

package main

import "errors"

// Unter Linux waere libnvidia-ml.so der Weg; der Linux-Agent ist heute nur Testbetrieb, dort bleibt nvidia-smi
// (gpu.go fragt dort auch Temperatur, Leistung, Luefter und Drosselgruende ab).
type nvmlDev struct{ gpuName string }

func openNVML() (*nvmlDev, error) { return nil, errors.New("NVML is only bound on Windows") }

func (d *nvmlDev) sample() (int, int, int, int, int, error) {
	return 0, 0, 0, 0, 0, errors.New("NVML not available")
}

func (d *nvmlDev) extra(*GPUSensors) {}

func (d *nvmlDev) limits() guardLimits { return guardLimits{Err: "NVML nicht verfuegbar"} }

func (d *nvmlDev) setLimit(float64) error { return errors.New("NVML nicht verfuegbar") }

func (d *nvmlDev) close() {}
