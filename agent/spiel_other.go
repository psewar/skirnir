//go:build !windows

package main

import (
	"bufio"
	"os"
	"strconv"
	"strings"
)

// Linux: keine Spielerkennung (die Knoten dort spielen nicht); der Heartbeat sagt das mit supported=false.
const gameScanSupported = false

func listProcesses() []procInfo { return nil }
func gameFolders() []string     { return nil }

// memoryFact: MemAvailable/MemTotal aus /proc/meminfo. Den Commit meldet Linux nur als Rechengroesse - bei der ueblichen
// Ueberbuchung (overcommit_memory 0) liegt Committed_AS oft ueber CommitLimit, ohne dass etwas knapp ist. Darum fehlt er.
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
