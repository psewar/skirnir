package main

import (
	"context"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// GPUSample holds raw measurements. Windows (WDDM) provides no per-process VRAM, so only totals; interpretation
// (foreign VRAM, busy) is done by the router. Sensors (0.6.0) carries temperature, power, throttling reasons, and - if
// GPU-Z is running - its extra values; the block is missing if the source cannot provide them.
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
	smi    string      // Path to nvidia-smi.exe (fallback) or "nvml"
	nvml   *nvmlDev    // NVML direkt (Windows), nil = nvidia-smi
	source string      // "nvml" | "nvidia-smi"
	gpuz   *gpuzReader // GPU-Z shared memory (Windows, optional), nil = off
	gpuzOn bool        // Config gpuz.enabled (default on); also applies to the relay
	// Relay: GPU-Z values sent by `gpuz-relay` from the login session via POST /gpuz when the service cannot open the object
	// itself (virtual service account). Valid for 15 s.
	relayMu    sync.Mutex
	relay      *GPUSensors
	relayAt    time.Time
	mu         sync.Mutex
	last       *GPUSample
	err        error
	failStreak int // NVML error counter -> after 3 consecutive, permanently fall back to nvidia-smi
}

// newGPU: NVML first (microseconds per measurement, no process start), otherwise nvidia-smi.exe as before.
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

// enableGPUZ toggles the GPU-Z reader (config gpuz.enabled, default on). Remains silent if GPU-Z is not running.
func (g *GPU) enableGPUZ(log *Logger) { g.gpuz, g.gpuzOn = newGPUZReader(log), true }

// SetRelayed accepts GPU-Z values from the relay (false = GPU-Z disabled via config).
func (g *GPU) SetRelayed(s *GPUSensors) bool {
	if !g.gpuzOn || s == nil || !s.GPUZ {
		return g.gpuzOn
	}
	g.relayMu.Lock()
	g.relay, g.relayAt = s, time.Now()
	g.relayMu.Unlock()
	return true
}

// RelayAge: age of the last relay values (for /health), 0 = never.
func (g *GPU) RelayAge() time.Duration {
	g.relayMu.Lock()
	defer g.relayMu.Unlock()
	if g.relayAt.IsZero() {
		return 0
	}
	return time.Since(g.relayAt)
}

// Source: measurement method (log, /health).
func (g *GPU) Source() string { g.mu.Lock(); defer g.mu.Unlock(); return g.source }

// dev returns the NVML device under lock (nil = nvidia-smi). Heartbeat, Guard, and /health call concurrently; the
// fallback to nvidia-smi sets g.nvml to nil - without a lock, this would be a nil access in the other caller (Review 2026-09-25).
func (g *GPU) dev() *nvmlDev { g.mu.Lock(); defer g.mu.Unlock(); return g.nvml }

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
	return "", fmt.Errorf("nvidia-smi.exe not found")
}

// smiFields: what we query from nvidia-smi (order = response columns). The first five are required,
// the rest may be "N/A".
var smiFields = []string{
	"utilization.gpu", "memory.total", "memory.used", "memory.free", "name",
	"temperature.gpu", "power.draw", "power.limit", "power.default_limit", "fan.speed", "utilization.memory",
	"clocks_throttle_reasons.active",
}

// Sample measures via NVML, otherwise via nvidia-smi (no console window), and stores the last value.
func (g *GPU) Sample(ctx context.Context) (*GPUSample, error) {
	if nv := g.dev(); nv != nil {
		util, memUtil, total, used, free, err := nv.sample()
		if err == nil {
			s := &GPUSample{Name: nv.gpuName, UtilPct: util, TotalMiB: total, UsedMiB: used, FreeMiB: free, At: time.Now(),
				Sensors: &GPUSensors{MemUtilPct: iptr(memUtil)}}
			nv.extra(s.Sensors)
			g.addGPUZ(s.Sensors)
			g.mu.Lock()
			g.last, g.err, g.failStreak = s, nil, 0
			g.mu.Unlock()
			return s, nil
		}
		g.mu.Lock()
		g.failStreak++
		giveUp := g.failStreak >= 3 && g.smi != "nvml" && g.nvml == nv
		if giveUp {
			g.nvml, g.source = nil, "nvidia-smi"
		}
		smiOnly := g.smi == "nvml"
		g.mu.Unlock()
		if giveUp {
			nv.close()
		} else if smiOnly {
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
		err := fmt.Errorf("unexpected nvidia-smi output: %q", line)
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

// smiSensors parses optional columns of the nvidia-smi response; "N/A" and "[N/A]" mean: no value.
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

// addGPUZ mixes in GPU-Z values: directly from shared memory if we can open it, otherwise fresh
// relay values from the login session.
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

// PowerLimits reads current, default, min, and max limits (Watt): via NVML, otherwise via nvidia-smi.
func (g *GPU) PowerLimits() guardLimits {
	if nv := g.dev(); nv != nil {
		return nv.limits()
	}
	if g.smi == "" || g.smi == "nvml" {
		return guardLimits{Err: "kein nvidia-smi"}
	}
	cctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, g.smi, "--query-gpu=power.limit,power.default_limit,power.min_limit,power.max_limit", "--format=csv,noheader,nounits")
	hideWindow(cmd)
	out, err := cmd.Output()
	if err != nil {
		return guardLimits{Err: "nvidia-smi: " + err.Error()}
	}
	f := strings.Split(strings.TrimSpace(strings.SplitN(string(out), "\n", 2)[0]), ",")
	if len(f) < 4 {
		return guardLimits{Err: "nvidia-smi: unerwartete Limit-Ausgabe"}
	}
	var v [4]float64
	for i := range v {
		x, err := strconv.ParseFloat(strings.Trim(f[i], "[] "), 64)
		if err != nil {
			return guardLimits{Err: "Power-Limit von nvidia-smi nicht lesbar (" + strings.TrimSpace(f[i]) + ")"}
		}
		v[i] = x
	}
	return guardLimits{Cur: v[0], Def: v[1], Min: v[2], Max: v[3], OK: true}
}

// SetPowerLimit sets the power limit (Watt): NVML (Admin) or `nvidia-smi -pl` (root). The Guard clamps the value
// to [Min, Default] beforehand; no further checks here.
func (g *GPU) SetPowerLimit(w float64) error {
	if nv := g.dev(); nv != nil {
		return nv.setLimit(w)
	}
	if g.smi == "" || g.smi == "nvml" {
		return fmt.Errorf("kein nvidia-smi")
	}
	cctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, g.smi, "-pl", strconv.Itoa(int(math.Round(w))))
	hideWindow(cmd)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("nvidia-smi -pl: %v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (g *GPU) setErr(err error) { g.mu.Lock(); g.err = err; g.mu.Unlock() }

// Last returns the last successful measurement (for MQTT and /health) without re-querying.
func (g *GPU) Last() (*GPUSample, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.last, g.err
}
