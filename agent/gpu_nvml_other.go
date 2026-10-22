//go:build !windows

package main

import "errors"

// On Linux, libnvidia-ml.so would be the way; the Linux agent is currently in test mode, so nvidia-smi remains there
// (gpu.go also queries temperature, power, fans, and throttling reasons there).
type nvmlDev struct{ gpuName string }

func openNVML() (*nvmlDev, error) { return nil, errors.New("NVML is only bound on Windows") }

func (d *nvmlDev) sample() (int, int, int, int, int, error) {
	return 0, 0, 0, 0, 0, errors.New("NVML not available")
}

func (d *nvmlDev) extra(*GPUSensors) {}

func (d *nvmlDev) limits() guardLimits { return guardLimits{Err: "NVML nicht verfuegbar"} }

func (d *nvmlDev) setLimit(float64) error { return errors.New("NVML nicht verfuegbar") }

func (d *nvmlDev) close() {}
