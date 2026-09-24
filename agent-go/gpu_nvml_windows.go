//go:build windows

package main

// NVML direkt aus nvml.dll (liegt bei jedem NVIDIA-Treiber in System32), ohne cgo ueber LazyDLL.
// Grund (2026-09-11, Performance-Runde): der Heartbeat startete alle 2 s nvidia-smi.exe - 25 ms CPU je Start,
// 43 200 Prozessstarts am Tag, ~1,2 % eines Kerns dauerhaft. Ein NVML-Aufruf kostet Mikrosekunden.
// nvidia-smi bleibt als Rueckfallweg (gpu.go), falls die DLL fehlt oder ein Aufruf scheitert.
//
// Seit 0.6.0 zusaetzlich (jeder Aufruf optional, fehlt er im Treiber, bleibt der Wert weg): Temperatur, Leistung,
// wirksames und Standard-Power-Limit, Luefter, Drosselgruende (Clocks-Event-Reasons-Bitmaske).

import (
	"fmt"
	"math"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

type nvmlUtil struct{ GPU, Memory uint32 }
type nvmlMem struct{ Total, Free, Used uint64 }

// nvmlMemory_v2: enthaelt Reserved; nvidia-smi rechnet Reserved NICHT zu used (Treiber >= 535). Damit die Werte zu den
// bisherigen nvidia-smi-Messungen (Baseline im Router) passen, bevorzugen wir v2 und fallen auf v1 zurueck.
type nvmlMem2 struct {
	Version  uint32
	Total    uint64
	Reserved uint64
	Free     uint64
	Used     uint64
}

type nvmlDev struct {
	dll      *windows.LazyDLL
	init     *windows.LazyProc
	shutdown *windows.LazyProc
	byIndex  *windows.LazyProc
	util     *windows.LazyProc
	memInfo  *windows.LazyProc
	memInfo2 *windows.LazyProc // nvmlDeviceGetMemoryInfo_v2, kann fehlen
	name     *windows.LazyProc
	// optional (0.6.0)
	temp     *windows.LazyProc
	power    *windows.LazyProc
	limit    *windows.LazyProc
	defLimit *windows.LazyProc
	fan      *windows.LazyProc
	throttle *windows.LazyProc
	handle   uintptr
	mu       sync.Mutex
	gpuName  string
}

func nvmlErr(what string, rc uintptr) error { return fmt.Errorf("NVML %s: Rueckgabe %d", what, rc) }

// optProc: Prozedur, die fehlen darf (aeltere Treiber) -> nil.
func optProc(dll *windows.LazyDLL, names ...string) *windows.LazyProc {
	for _, n := range names {
		p := dll.NewProc(n)
		if p.Find() == nil {
			return p
		}
	}
	return nil
}

// openNVML initialisiert NVML einmal und holt das Handle der ersten GPU. Fehler = Rueckfall auf nvidia-smi.
func openNVML() (*nvmlDev, error) {
	d := &nvmlDev{dll: windows.NewLazySystemDLL("nvml.dll")}
	if err := d.dll.Load(); err != nil {
		return nil, fmt.Errorf("nvml.dll: %w", err)
	}
	d.init = d.dll.NewProc("nvmlInit_v2")
	d.shutdown = d.dll.NewProc("nvmlShutdown")
	d.byIndex = d.dll.NewProc("nvmlDeviceGetHandleByIndex_v2")
	d.util = d.dll.NewProc("nvmlDeviceGetUtilizationRates")
	d.memInfo = d.dll.NewProc("nvmlDeviceGetMemoryInfo")
	d.memInfo2 = optProc(d.dll, "nvmlDeviceGetMemoryInfo_v2")
	d.name = d.dll.NewProc("nvmlDeviceGetName")
	d.temp = optProc(d.dll, "nvmlDeviceGetTemperature")
	d.power = optProc(d.dll, "nvmlDeviceGetPowerUsage")
	d.limit = optProc(d.dll, "nvmlDeviceGetEnforcedPowerLimit", "nvmlDeviceGetPowerManagementLimit")
	d.defLimit = optProc(d.dll, "nvmlDeviceGetPowerManagementDefaultLimit")
	d.fan = optProc(d.dll, "nvmlDeviceGetFanSpeed")
	d.throttle = optProc(d.dll, "nvmlDeviceGetCurrentClocksEventReasons", "nvmlDeviceGetCurrentClocksThrottleReasons")
	for _, p := range []*windows.LazyProc{d.init, d.shutdown, d.byIndex, d.util, d.memInfo, d.name} {
		if err := p.Find(); err != nil {
			return nil, err
		}
	}
	if rc, _, _ := d.init.Call(); rc != 0 {
		return nil, nvmlErr("Init", rc)
	}
	if rc, _, _ := d.byIndex.Call(0, uintptr(unsafe.Pointer(&d.handle))); rc != 0 {
		d.shutdown.Call()
		return nil, nvmlErr("GetHandleByIndex", rc)
	}
	buf := make([]byte, 96)
	if rc, _, _ := d.name.Call(d.handle, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf))); rc == 0 {
		n := 0
		for n < len(buf) && buf[n] != 0 {
			n++
		}
		d.gpuName = string(buf[:n])
	}
	return d, nil
}

// sample liest Auslastung (GPU und Speichercontroller) und Speicher; Werte wie nvidia-smi --query-gpu (MiB, Prozent).
func (d *nvmlDev) sample() (util, memUtil int, totalMiB, usedMiB, freeMiB int, err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	var u nvmlUtil
	if rc, _, _ := d.util.Call(d.handle, uintptr(unsafe.Pointer(&u))); rc != 0 {
		return 0, 0, 0, 0, 0, nvmlErr("GetUtilizationRates", rc)
	}
	const mib = 1024 * 1024
	if d.memInfo2 != nil {
		m2 := nvmlMem2{Version: uint32(unsafe.Sizeof(nvmlMem2{})) | 2<<24}
		if rc, _, _ := d.memInfo2.Call(d.handle, uintptr(unsafe.Pointer(&m2))); rc == 0 {
			return int(u.GPU), int(u.Memory), int(m2.Total / mib), int(m2.Used / mib), int(m2.Free / mib), nil
		}
		d.memInfo2 = nil // dieser Treiber kann v2 nicht -> ab jetzt v1
	}
	var m nvmlMem
	if rc, _, _ := d.memInfo.Call(d.handle, uintptr(unsafe.Pointer(&m))); rc != 0 {
		return 0, 0, 0, 0, 0, nvmlErr("GetMemoryInfo", rc)
	}
	return int(u.GPU), int(u.Memory), int(m.Total / mib), int(m.Used / mib), int(m.Free / mib), nil
}

func mw(v uint32) *float64 { return fptr(math.Round(float64(v)/100) / 10) } // Milliwatt -> Watt, 1 Nachkommastelle

// extra fuellt die optionalen Sensoren. Ein Aufruf, den der Treiber ablehnt (z. B. Luefter bei passiven Karten),
// laesst nur seinen Wert weg; nichts davon ist ein Fehler des Samples.
func (d *nvmlDev) extra(s *GPUSensors) {
	d.mu.Lock()
	defer d.mu.Unlock()
	var v32 uint32
	if d.temp != nil {
		if rc, _, _ := d.temp.Call(d.handle, 0 /* NVML_TEMPERATURE_GPU */, uintptr(unsafe.Pointer(&v32))); rc == 0 {
			s.TempC = iptr(int(v32))
		}
	}
	if d.power != nil {
		if rc, _, _ := d.power.Call(d.handle, uintptr(unsafe.Pointer(&v32))); rc == 0 {
			s.PowerW = mw(v32)
		}
	}
	if d.limit != nil {
		if rc, _, _ := d.limit.Call(d.handle, uintptr(unsafe.Pointer(&v32))); rc == 0 {
			s.PowerLimitW = mw(v32)
		}
	}
	if d.defLimit != nil {
		if rc, _, _ := d.defLimit.Call(d.handle, uintptr(unsafe.Pointer(&v32))); rc == 0 {
			s.PowerLimitDefW = mw(v32)
		}
	}
	if d.fan != nil {
		if rc, _, _ := d.fan.Call(d.handle, uintptr(unsafe.Pointer(&v32))); rc == 0 {
			s.FanPct = iptr(int(v32))
		}
	}
	if d.throttle != nil {
		var mask uint64
		if rc, _, _ := d.throttle.Call(d.handle, uintptr(unsafe.Pointer(&mask))); rc == 0 {
			s.ThrottleMask, s.ThrottleReasons = &mask, decodeThrottle(mask)
		}
	}
}

func (d *nvmlDev) close() {
	if d != nil && d.shutdown != nil {
		d.shutdown.Call()
	}
}
