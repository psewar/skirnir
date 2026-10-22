//go:build windows

package main

import (
	"fmt"
	"math"
	"strconv"
	"unsafe"

	"golang.org/x/sys/windows"
)

// GPU-Bedarf je Prozess fuer die Spielerkennung (0.19.0): eigener und geteilter Grafikspeicher (GPU Process Memory) und die
// Auslastung der GPU-Engines (GPU Engine, Instanzen pid_<PID>_luid_..._engtype_<Typ>). Die Engine-Auslastung ist eine Rate -
// PDH liefert sie erst ab dem zweiten Sammeln derselben Abfrage, darum bleibt die Abfrage offen, solange ein Spiel laeuft.
// Gemessen 2026-10-21 an einem kleinen Spiel: 735 MiB eigen, 32 MiB geteilt, 1,8 % - der Task-Manager zeigt dasselbe.

const (
	pdhFmtDouble      = 0x00000200
	pdhCstatusValid   = 0
	pdhCstatusNewData = 1
	gpuProcShared     = `\GPU Process Memory(*)\Shared Usage`
	gpuEngineUtil     = `\GPU Engine(*)\Utilization Percentage`
)

// PDH_FMT_COUNTERVALUE_ITEM_W mit doubleValue (gleiche Lage wie pdhItemLarge, die Union ist 8 Bytes gross)
type pdhItemDouble struct {
	Name    *uint16
	CStatus uint32
	_       uint32
	Double  float64
}

type pdhGPUSampler struct {
	q, ded, shared, util windows.Handle
}

func newGPUSampler() (gpuSampler, error) {
	if err := pdhDLL.Load(); err != nil {
		return nil, fmt.Errorf("pdh.dll: %w", err)
	}
	s := &pdhGPUSampler{}
	if r, _, _ := procPdhOpenQueryW.Call(0, 0, uintptr(unsafe.Pointer(&s.q))); r != 0 {
		return nil, fmt.Errorf("PdhOpenQuery: 0x%x", r)
	}
	for _, c := range []struct {
		path string
		h    *windows.Handle
	}{{gpuProcCount, &s.ded}, {gpuProcShared, &s.shared}, {gpuEngineUtil, &s.util}} {
		p, _ := windows.UTF16PtrFromString(c.path)
		if r, _, _ := procPdhAddEnglishCounterW.Call(uintptr(s.q), uintptr(unsafe.Pointer(p)), 0, uintptr(unsafe.Pointer(c.h))); r != 0 {
			s.close()
			return nil, fmt.Errorf("PdhAddEnglishCounter %s: 0x%x", c.path, r)
		}
	}
	return s, nil
}

func (s *pdhGPUSampler) close() {
	if s.q != 0 {
		procPdhCloseQuery.Call(uintptr(s.q))
		s.q = 0
	}
}

func (s *pdhGPUSampler) sample() (gpuUse, error) {
	u := gpuUse{dedicated: map[uint32]uint64{}, shared: map[uint32]uint64{}, pct: map[uint32]float64{}}
	if r, _, _ := procPdhCollectQueryData.Call(uintptr(s.q)); r != 0 {
		return u, fmt.Errorf("PdhCollectQueryData: 0x%x", r)
	}
	for _, c := range []struct {
		h   windows.Handle
		out map[uint32]uint64
	}{{s.ded, u.dedicated}, {s.shared, u.shared}} {
		buf, n, err := pdhArray(c.h, pdhFmtLarge)
		if err != nil {
			return u, err
		}
		for _, it := range unsafe.Slice((*pdhItemLarge)(unsafe.Pointer(&buf[0])), n) {
			if pid, ok := instancePID(it.Name); ok && it.Large > 0 && (it.CStatus == pdhCstatusValid || it.CStatus == pdhCstatusNewData) {
				c.out[pid] += uint64(it.Large)
			}
		}
	}
	// Auslastung: beim ersten Sammeln gibt es noch keine Rate - dann bleibt pct leer, das ist kein Fehler
	if buf, n, err := pdhArray(s.util, pdhFmtDouble); err == nil {
		for _, it := range unsafe.Slice((*pdhItemDouble)(unsafe.Pointer(&buf[0])), n) {
			pid, ok := instancePID(it.Name)
			if !ok || (it.CStatus != pdhCstatusValid && it.CStatus != pdhCstatusNewData) || math.IsNaN(it.Double) {
				continue
			}
			if v, seen := u.pct[pid]; !seen || it.Double > v { // je PID die am staerksten ausgelastete Engine
				u.pct[pid] = it.Double
			}
		}
	}
	return u, nil
}

// pdhArray: formatierte Werte aller Instanzen eines Zaehlers. Keine Instanz (kein Prozess nutzt die GPU) = leere Liste.
func pdhArray(c windows.Handle, format uintptr) ([]byte, uint32, error) {
	var size, n uint32
	r, _, _ := procPdhGetFormattedCounterAr.Call(uintptr(c), format, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&n)), 0)
	if r == 0 {
		return make([]byte, 1), 0, nil
	}
	if r != pdhMoreData {
		return nil, 0, fmt.Errorf("PdhGetFormattedCounterArray (size): 0x%x", r)
	}
	buf := make([]byte, size)
	if r, _, _ := procPdhGetFormattedCounterAr.Call(uintptr(c), format, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&n)),
		uintptr(unsafe.Pointer(&buf[0]))); r != 0 {
		return nil, 0, fmt.Errorf("PdhGetFormattedCounterArray: 0x%x", r)
	}
	return buf, n, nil
}

// instancePID: PID aus dem Instanznamen pid_<PID>_luid_...
func instancePID(name *uint16) (uint32, bool) {
	m := gpuInstPID.FindStringSubmatch(windows.UTF16PtrToString(name))
	if m == nil {
		return 0, false
	}
	pid, err := strconv.ParseUint(m[1], 10, 32)
	return uint32(pid), err == nil
}
