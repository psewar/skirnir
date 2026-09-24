//go:build !windows

package main

import "errors"

// Unter Linux waere libnvidia-ml.so der Weg; der Linux-Agent ist heute nur Testbetrieb, dort bleibt nvidia-smi
// (gpu.go fragt dort auch Temperatur, Leistung, Luefter und Drosselgruende ab).
type nvmlDev struct{ gpuName string }

func openNVML() (*nvmlDev, error) { return nil, errors.New("NVML nur unter Windows angebunden") }

func (d *nvmlDev) sample() (int, int, int, int, int, error) {
	return 0, 0, 0, 0, 0, errors.New("NVML nicht verfuegbar")
}

func (d *nvmlDev) extra(*GPUSensors) {}

func (d *nvmlDev) close() {}
