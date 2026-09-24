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

// GPUSample sind die rohen Messwerte. Windows (WDDM) liefert kein Prozess-VRAM, deshalb nur Gesamtwerte; die Deutung
// (fremdes VRAM, busy) macht der Router. Sensors (0.6.0) traegt Temperatur, Leistung, Drosselgruende und - falls
// GPU-Z laeuft - dessen Zusatzwerte; der Block fehlt, wenn die Quelle nichts davon kann.
type GPUSample struct {
	Name     string      `json:"name"`
	UtilPct  int         `json:"gpu_util_pct"`
	TotalMiB int         `json:"vram_total_mib"`
	UsedMiB  int         `json:"vram_used_mib"`
	FreeMiB  int         `json:"vram_free_mib"`
	At       time.Time   `json:"at"`
	Sensors  *GPUSensors `json:"sensors,omitempty"`
}

type GPU struct {
	smi    string      // Pfad zu nvidia-smi.exe (Rueckfallweg) oder "nvml"
	nvml   *nvmlDev    // NVML direkt (Windows), nil = nvidia-smi
	source string      // "nvml" | "nvidia-smi"
	gpuz   *gpuzReader // GPU-Z-Shared-Memory (Windows, optional), nil = aus
	gpuzOn bool        // Konfiguration gpuz.enabled (Standard an); gilt auch fuer das Relay
	// Relay: GPU-Z-Werte, die `gpuz-relay` aus der Anmeldesitzung per POST /gpuz schickt, wenn der Dienst das Objekt
	// selbst nicht oeffnen darf (virtuelles Dienstkonto). Gelten 15 s.
	relayMu sync.Mutex
	relay   *GPUSensors
	relayAt time.Time
	mu      sync.Mutex
	last    *GPUSample
	err     error
	falls   int // Zaehler NVML-Fehler -> nach 3 in Folge dauerhaft auf nvidia-smi
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

// enableGPUZ schaltet den GPU-Z-Leser zu (Konfiguration gpuz.enabled, Standard an). Ohne laufendes GPU-Z bleibt er still.
func (g *GPU) enableGPUZ(log *Logger) { g.gpuz, g.gpuzOn = newGPUZReader(log), true }

// SetRelayed nimmt GPU-Z-Werte vom Relay an (false = GPU-Z ist per Konfiguration aus).
func (g *GPU) SetRelayed(s *GPUSensors) bool {
	if !g.gpuzOn || s == nil || !s.GPUZ {
		return g.gpuzOn
	}
	g.relayMu.Lock()
	g.relay, g.relayAt = s, time.Now()
	g.relayMu.Unlock()
	return true
}

// RelayAge: Alter der letzten Relay-Werte (fuer /health), 0 = nie.
func (g *GPU) RelayAge() time.Duration {
	g.relayMu.Lock()
	defer g.relayMu.Unlock()
	if g.relayAt.IsZero() {
		return 0
	}
	return time.Since(g.relayAt)
}

// Source: womit gemessen wird (Log, /health).
func (g *GPU) Source() string { return g.source }

// GPUZActive: ob im letzten Sample frische GPU-Z-Werte waren.
func (g *GPU) GPUZActive() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.last != nil && g.last.Sensors.hasGPUZ()
}

func findNvidiaSmi() (string, error) {
	if p, err := exec.LookPath("nvidia-smi.exe"); err == nil {
		return p, nil
	}
	if p, err := exec.LookPath("nvidia-smi"); err == nil {
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

// smiFields: was wir nvidia-smi abfragen (Reihenfolge = Spalten der Antwort). Die ersten fuenf sind Pflicht,
// der Rest darf "N/A" sein.
var smiFields = []string{
	"utilization.gpu", "memory.total", "memory.used", "memory.free", "name",
	"temperature.gpu", "power.draw", "power.limit", "power.default_limit", "fan.speed", "utilization.memory",
	"clocks_throttle_reasons.active",
}

// Sample misst per NVML, sonst per nvidia-smi (ohne Konsolenfenster), und merkt sich den letzten Wert.
func (g *GPU) Sample(ctx context.Context) (*GPUSample, error) {
	if g.nvml != nil {
		util, memUtil, total, used, free, err := g.nvml.sample()
		if err == nil {
			s := &GPUSample{Name: g.nvml.gpuName, UtilPct: util, TotalMiB: total, UsedMiB: used, FreeMiB: free, At: time.Now(),
				Sensors: &GPUSensors{MemUtilPct: iptr(memUtil)}}
			g.nvml.extra(s.Sensors)
			g.addGPUZ(s.Sensors)
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
	cmd := exec.CommandContext(cctx, g.smi, "--query-gpu="+strings.Join(smiFields, ","), "--format=csv,noheader,nounits")
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
	for i := range f {
		f[i] = strings.TrimSpace(f[i])
	}
	s := &GPUSample{Name: f[4], At: time.Now()}
	s.UtilPct, _ = strconv.Atoi(f[0])
	s.TotalMiB, _ = strconv.Atoi(f[1])
	s.UsedMiB, _ = strconv.Atoi(f[2])
	s.FreeMiB, _ = strconv.Atoi(f[3])
	s.Sensors = smiSensors(f)
	g.addGPUZ(s.Sensors)
	g.mu.Lock()
	g.last, g.err = s, nil
	g.mu.Unlock()
	return s, nil
}

// smiSensors deutet die optionalen Spalten der nvidia-smi-Antwort; "N/A" und "[N/A]" heissen: kein Wert.
func smiSensors(f []string) *GPUSensors {
	s := &GPUSensors{}
	get := func(i int) (string, bool) {
		if i >= len(f) {
			return "", false
		}
		v := strings.Trim(f[i], "[] ")
		if v == "" || strings.EqualFold(v, "N/A") || strings.HasPrefix(v, "Not Supported") {
			return "", false
		}
		return v, true
	}
	if v, ok := get(5); ok {
		if n, err := strconv.Atoi(v); err == nil {
			s.TempC = iptr(n)
		}
	}
	for i, dst := range []**float64{&s.PowerW, &s.PowerLimitW, &s.PowerLimitDefW} {
		if v, ok := get(6 + i); ok {
			if x, err := strconv.ParseFloat(v, 64); err == nil {
				*dst = fptr(x)
			}
		}
	}
	if v, ok := get(9); ok {
		if n, err := strconv.Atoi(v); err == nil {
			s.FanPct = iptr(n)
		}
	}
	if v, ok := get(10); ok {
		if n, err := strconv.Atoi(v); err == nil {
			s.MemUtilPct = iptr(n)
		}
	}
	if v, ok := get(11); ok {
		if mask, err := strconv.ParseUint(strings.TrimPrefix(strings.ToLower(v), "0x"), 16, 64); err == nil {
			s.ThrottleMask, s.ThrottleReasons = &mask, decodeThrottle(mask)
		}
	}
	return s
}

// addGPUZ mischt die GPU-Z-Werte dazu: direkt aus dem Shared Memory, wenn wir es oeffnen duerfen, sonst die frischen
// Werte des Relays aus der Anmeldesitzung.
func (g *GPU) addGPUZ(s *GPUSensors) {
	if !g.gpuzOn || s == nil {
		return
	}
	if g.gpuz != nil {
		if d, err := g.gpuz.read(); err == nil && d.apply(s) {
			return
		}
	}
	g.relayMu.Lock()
	r, at := g.relay, g.relayAt
	g.relayMu.Unlock()
	if r != nil && time.Since(at) < 15*time.Second {
		s.mergeGPUZ(r)
	}
}

func (g *GPU) setErr(err error) { g.mu.Lock(); g.err = err; g.mu.Unlock() }

// Last liefert den letzten erfolgreichen Messwert (fuer MQTT und /health), ohne neu abzufragen.
func (g *GPU) Last() (*GPUSample, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.last, g.err
}
