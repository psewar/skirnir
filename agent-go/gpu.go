package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// GPUSample sind die rohen nvidia-smi-Werte. Windows (WDDM) liefert kein Prozess-VRAM,
// deshalb nur Gesamtwerte; die Deutung (fremdes VRAM, busy) macht der Router.
type GPUSample struct {
	Name     string    `json:"name"`
	UtilPct  int       `json:"gpu_util_pct"`
	TotalMiB int       `json:"vram_total_mib"`
	UsedMiB  int       `json:"vram_used_mib"`
	FreeMiB  int       `json:"vram_free_mib"`
	At       time.Time `json:"at"`
}

type GPU struct {
	smi    string   // Pfad zu nvidia-smi.exe (Rueckfallweg) oder "nvml"
	nvml   *nvmlDev // NVML direkt (Windows), nil = nvidia-smi
	source string   // "nvml" | "nvidia-smi"
	mu     sync.Mutex
	last   *GPUSample
	err    error
	falls  int // Zaehler NVML-Fehler -> nach 3 in Folge dauerhaft auf nvidia-smi
}

// newGPU: NVML zuerst (Mikrosekunden je Messung, kein Prozessstart), sonst nvidia-smi.exe wie bisher.
func newGPU() (*GPU, error) {
	g := &GPU{}
	if d, err := openNVML(); err == nil {
		g.nvml, g.source, g.smi = d, "nvml", "nvml"
		if p, err := findNvidiaSmi(); err == nil {
			g.smi = p // Rueckfallweg merken
		}
		return g, nil
	}
	p, err := findNvidiaSmi()
	if err != nil {
		return nil, err
	}
	g.smi, g.source = p, "nvidia-smi"
	return g, nil
}

// Source: womit gemessen wird (Log, /health).
func (g *GPU) Source() string { return g.source }

func findNvidiaSmi() (string, error) {
	if p, err := exec.LookPath("nvidia-smi.exe"); err == nil {
		return p, nil
	}
	for _, c := range []string{
		filepath.Join(os.Getenv("SystemRoot"), "System32", "nvidia-smi.exe"),
		`C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe`,
	} {
		if _, err := os.Stat(c); err == nil {
			return c, nil
		}
	}
	return "", fmt.Errorf("nvidia-smi.exe nicht gefunden")
}

// Sample misst per NVML, sonst per nvidia-smi (ohne Konsolenfenster), und merkt sich den letzten Wert.
func (g *GPU) Sample(ctx context.Context) (*GPUSample, error) {
	if g.nvml != nil {
		util, total, used, free, err := g.nvml.sample()
		if err == nil {
			s := &GPUSample{Name: g.nvml.gpuName, UtilPct: util, TotalMiB: total, UsedMiB: used, FreeMiB: free, At: time.Now()}
			g.mu.Lock()
			g.last, g.err, g.falls = s, nil, 0
			g.mu.Unlock()
			return s, nil
		}
		g.mu.Lock()
		g.falls++
		giveUp := g.falls >= 3 && g.smi != "nvml"
		g.mu.Unlock()
		if giveUp {
			g.nvml.close()
			g.nvml, g.source = nil, "nvidia-smi"
		} else if g.smi == "nvml" {
			g.setErr(err)
			return nil, err
		}
	}
	cctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, g.smi, "--query-gpu=utilization.gpu,memory.total,memory.used,memory.free,name",
		"--format=csv,noheader,nounits")
	hideWindow(cmd)
	out, err := cmd.Output()
	if err != nil {
		g.setErr(err)
		return nil, err
	}
	line := strings.TrimSpace(strings.SplitN(string(out), "\n", 2)[0])
	f := strings.Split(line, ",")
	if len(f) < 5 {
		err := fmt.Errorf("unerwartete nvidia-smi-Ausgabe: %q", line)
		g.setErr(err)
		return nil, err
	}
	s := &GPUSample{Name: strings.TrimSpace(f[4]), At: time.Now()}
	s.UtilPct, _ = strconv.Atoi(strings.TrimSpace(f[0]))
	s.TotalMiB, _ = strconv.Atoi(strings.TrimSpace(f[1]))
	s.UsedMiB, _ = strconv.Atoi(strings.TrimSpace(f[2]))
	s.FreeMiB, _ = strconv.Atoi(strings.TrimSpace(f[3]))
	g.mu.Lock()
	g.last, g.err = s, nil
	g.mu.Unlock()
	return s, nil
}

func (g *GPU) setErr(err error) { g.mu.Lock(); g.err = err; g.mu.Unlock() }

// Last liefert den letzten erfolgreichen Messwert (fuer MQTT und /health), ohne neu abzufragen.
func (g *GPU) Last() (*GPUSample, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.last, g.err
}
