package main

import (
	"fmt"
	"strings"
)

// GPUSensors are the additional values since 0.6.0: what NVML provides beyond utilization and VRAM (temperature, power,
// limit, fans, throttle reasons) and - only when GPU-Z is running - its shared memory sensors (memory temperature,
// hot spot, voltage, 16-pin power). Each value is optional (nil = not measurable on this node); the router
// takes the block unchanged into the node state, the MQTT device turns it into HA sensors.
type GPUSensors struct {
	// NVML (works without login, also as a service)
	TempC           *int     `json:"temp_c,omitempty"`
	PowerW          *float64 `json:"power_w,omitempty"`
	PowerLimitW     *float64 `json:"power_limit_w,omitempty"`
	PowerLimitDefW  *float64 `json:"power_limit_default_w,omitempty"`
	FanPct          *int     `json:"fan_pct,omitempty"`
	MemUtilPct      *int     `json:"mem_util_pct,omitempty"`
	ThrottleMask    *uint64  `json:"throttle_mask,omitempty"`
	ThrottleReasons []string `json:"throttle_reasons,omitempty"`
	// GPU-Z (Windows only, only when the program is running; source = "gpuz")
	MemTempC      *float64 `json:"mem_temp_c,omitempty"`
	HotSpotC      *float64 `json:"hotspot_c,omitempty"`
	GPUVoltageV   *float64 `json:"gpu_voltage_v,omitempty"`
	Pin16PowerW   *float64 `json:"pin16_power_w,omitempty"`
	Pin16VoltageV *float64 `json:"pin16_voltage_v,omitempty"`
	BoardPowerW   *float64 `json:"board_power_w,omitempty"`
	PerfCapMask   *int     `json:"perfcap_mask,omitempty"`
	PerfCapReason []string `json:"perfcap_reasons,omitempty"`
	CPUTempC      *float64 `json:"cpu_temp_c,omitempty"`
	GPUZ          bool     `json:"gpuz,omitempty"` // true = GPU-Z-Werte in diesem Sample frisch
}

// nvmlThrottleBits: nvmlClocksThrottleReasons (nvml.h). Idle is not a problem, everything else means "throttled because".
var nvmlThrottleBits = []struct {
	bit  uint64
	name string
}{
	{0x1, "idle"}, {0x2, "app_clocks"}, {0x4, "sw_power_cap"}, {0x8, "hw_slowdown"}, {0x10, "sync_boost"},
	{0x20, "sw_thermal"}, {0x40, "hw_thermal"}, {0x80, "hw_power_brake"}, {0x100, "display_clocks"},
}

func decodeThrottle(mask uint64) []string {
	var out []string
	rest := mask
	for _, b := range nvmlThrottleBits {
		if mask&b.bit != 0 {
			out = append(out, b.name)
			rest &^= b.bit
		}
	}
	if rest != 0 { // keep undocumented bits visible (seen on an RTX 5090: 0x400 with GPU-Z PerfCap "vRel")
		out = append(out, fmt.Sprintf("bit_0x%x", rest))
	}
	return out
}

// gpuzPerfCapBits: meaning of the PerfCap bits as displayed by GPU-Z (Pwr, Thrm, vRel, VOp, Util, SLI). Mapping based on
// common readers of the NVAPI Perf Policy block; no official docs - so the raw number is passed along.
var gpuzPerfCapBits = []struct {
	bit  int
	name string
}{
	{1, "power"}, {2, "thermal"}, {4, "voltage_reliability"}, {8, "voltage_operating"}, {16, "utilization"}, {32, "sli"},
}

func decodePerfCap(mask int) []string {
	var out []string
	for _, b := range gpuzPerfCapBits {
		if mask&b.bit != 0 {
			out = append(out, b.name)
		}
	}
	return out
}

// mqttState writes the values to the MQTT state. Missing values are sent as null so HA shows "unknown"
// instead of keeping the last value.
func (s *GPUSensors) mqttState(st map[string]any) {
	put := func(k string, v any) { st[k] = v }
	if s == nil {
		s = &GPUSensors{}
	}
	put("gpu_temp_c", ival(s.TempC))
	put("gpu_power_w", fval(s.PowerW))
	put("gpu_power_limit_w", fval(s.PowerLimitW))
	put("gpu_fan_pct", ival(s.FanPct))
	if s.ThrottleMask != nil {
		r := s.ThrottleReasons
		if len(r) == 0 {
			r = []string{"none"}
		}
		put("gpu_throttle", strings.Join(r, ", "))
	} else {
		put("gpu_throttle", nil)
	}
	put("gpu_mem_temp_c", fval(s.MemTempC))
	put("gpu_hotspot_c", fval(s.HotSpotC))
	put("gpu_voltage_v", fval(s.GPUVoltageV))
	put("gpu_pin16_power_w", fval(s.Pin16PowerW))
	put("gpu_pin16_voltage_v", fval(s.Pin16VoltageV))
	put("cpu_temp_c", fval(s.CPUTempC))
	if s.PerfCapMask != nil {
		r := s.PerfCapReason
		if len(r) == 0 {
			r = []string{"none"}
		}
		put("gpu_perfcap", strings.Join(r, ", "))
	} else {
		put("gpu_perfcap", nil)
	}
}

// mergeGPUZ takes over the GPU-Z fields from src (e.g. from the Relay from the login session) into s.
func (s *GPUSensors) mergeGPUZ(src *GPUSensors) {
	if s == nil || src == nil || !src.GPUZ {
		return
	}
	s.MemTempC, s.HotSpotC, s.GPUVoltageV = src.MemTempC, src.HotSpotC, src.GPUVoltageV
	s.Pin16PowerW, s.Pin16VoltageV, s.BoardPowerW = src.Pin16PowerW, src.Pin16VoltageV, src.BoardPowerW
	s.PerfCapMask, s.PerfCapReason, s.CPUTempC = src.PerfCapMask, src.PerfCapReason, src.CPUTempC
	s.GPUZ = true
}

// hasNVML / hasGPUZ: whether the respective group exists on this node at all (for the HA entity groups).
func (s *GPUSensors) hasNVML() bool { return s != nil && (s.TempC != nil || s.PowerW != nil) }
func (s *GPUSensors) hasGPUZ() bool { return s != nil && s.GPUZ }

func fptr(v float64) *float64 { return &v }
func iptr(v int) *int         { return &v }

// fval/ival: pointer -> value or true nil (a typed nil pointer in an any would not be nil for comparisons).
func fval(p *float64) any {
	if p == nil {
		return nil
	}
	return *p
}

func ival(p *int) any {
	if p == nil {
		return nil
	}
	return *p
}
