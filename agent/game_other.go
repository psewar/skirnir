//go:build !windows

package main

import (
	"bufio"
	"errors"
	"os"
	"strconv"
	"strings"
)

// Linux: no player detection (nodes there do not play); the heartbeat reports supported=false.
const gameScanSupported = false

func listProcesses() []procInfo { return nil }
func gameFolders() []string     { return nil }

// newGPUSampler: without player detection, there is no per-game GPU demand to measure.
func newGPUSampler() (gpuSampler, error) {
	return nil, errors.New("GPU demand per process only on Windows")
}

// memoryFact: MemAvailable/MemTotal from /proc/meminfo. Linux only reports Commit as a calculated value - with typical
// overcommit (overcommit_memory 0), Committed_AS often exceeds CommitLimit without anything being tight. Hence it is omitted.
func memoryFact() (MemFact, bool) {
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return MemFact{}, false
	}
	defer f.Close()
	kib := map[string]uint64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		parts := strings.Fields(sc.Text())
		if len(parts) >= 2 {
			if v, err := strconv.ParseUint(parts[1], 10, 64); err == nil {
				kib[strings.TrimSuffix(parts[0], ":")] = v
			}
		}
	}
	avail, ok1 := kib["MemAvailable"]
	total, ok2 := kib["MemTotal"]
	if !ok1 || !ok2 {
		return MemFact{}, false
	}
	return MemFact{RAMAvailableGiB: gib(avail << 10), RAMTotalGiB: gib(total << 10)}, true
}
