//go:build windows

package main

// NVML direkt aus nvml.dll (liegt bei jedem NVIDIA-Treiber in System32), ohne cgo ueber LazyDLL.
// Grund (2026-09-11, Performance-Runde): der Heartbeat startete alle 2 s nvidia-smi.exe - 25 ms CPU je Start,
// 43 200 Prozessstarts am Tag, ~1,2 % eines Kerns dauerhaft. Ein NVML-Aufruf kostet Mikrosekunden.
// nvidia-smi bleibt als Rueckfallweg (gpu.go), falls die DLL fehlt oder ein Aufruf scheitert.

import (
	"fmt"
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
	handle   uintptr
	mu       sync.Mutex
	gpuName  string
}

func nvmlErr(what string, rc uintptr) error { return fmt.Errorf("NVML %s: Rueckgabe %d", what, rc) }

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
	d.memInfo2 = d.dll.NewProc("nvmlDeviceGetMemoryInfo_v2")
	if d.memInfo2.Find() != nil {
		d.memInfo2 = nil
	}
	d.name = d.dll.NewProc("nvmlDeviceGetName")
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

// sample liest Auslastung und Speicher; Werte wie nvidia-smi --query-gpu (MiB, Prozent).
func (d *nvmlDev) sample() (util int, totalMiB, usedMiB, freeMiB int, err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	var u nvmlUtil
	if rc, _, _ := d.util.Call(d.handle, uintptr(unsafe.Pointer(&u))); rc != 0 {
		return 0, 0, 0, 0, nvmlErr("GetUtilizationRates", rc)
	}
	const mib = 1024 * 1024
	if d.memInfo2 != nil {
		m2 := nvmlMem2{Version: uint32(unsafe.Sizeof(nvmlMem2{})) | 2<<24}
		if rc, _, _ := d.memInfo2.Call(d.handle, uintptr(unsafe.Pointer(&m2))); rc == 0 {
			return int(u.GPU), int(m2.Total / mib), int(m2.Used / mib), int(m2.Free / mib), nil
		}
		d.memInfo2 = nil // dieser Treiber kann v2 nicht -> ab jetzt v1
	}
	var m nvmlMem
	if rc, _, _ := d.memInfo.Call(d.handle, uintptr(unsafe.Pointer(&m))); rc != 0 {
		return 0, 0, 0, 0, nvmlErr("GetMemoryInfo", rc)
	}
	return int(u.GPU), int(m.Total / mib), int(m.Used / mib), int(m.Free / mib), nil
}

func (d *nvmlDev) close() {
	if d != nil && d.shutdown != nil {
		d.shutdown.Call()
	}
}
