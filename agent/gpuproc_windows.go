//go:build windows

package main

import (
	"fmt"
	"regexp"
	"strconv"
	"unsafe"

	"golang.org/x/sys/windows"
)

// GPU memory per process (0.13.0). nvidia-smi shows "N/A" per process under WDDM; the performance counter
// "\GPU Process Memory(*)\Dedicated Usage" has the values - per instance pid_<PID>_luid_<Adapter>_phys_<n>. Thus the
// agent reports the memory of its own children (STT via CUDA, 2026-09-30) as a fact, instead of the router treating it as foreign
// (game): 2.6 GiB of Whisper would otherwise push the node over the foreign threshold, even though only Desktop + game were running.
// Summed over all adapters; the iGPU has little "dedicated" memory, so the error is small.

var (
	pdhDLL                       = windows.NewLazySystemDLL("pdh.dll")
	procPdhOpenQueryW            = pdhDLL.NewProc("PdhOpenQueryW")
	procPdhAddEnglishCounterW    = pdhDLL.NewProc("PdhAddEnglishCounterW")
	procPdhCollectQueryData      = pdhDLL.NewProc("PdhCollectQueryData")
	procPdhGetFormattedCounterAr = pdhDLL.NewProc("PdhGetFormattedCounterArrayW")
	procPdhCloseQuery            = pdhDLL.NewProc("PdhCloseQuery")
	gpuInstPID                   = regexp.MustCompile(`^pid_(\d+)_`)
)

const (
	pdhFmtLarge  = 0x00000400
	pdhMoreData  = 0x800007D2
	gpuProcCount = `\GPU Process Memory(*)\Dedicated Usage`
)

// PDH_FMT_COUNTERVALUE_ITEM_W: Name, CStatus, (Ausrichtung), Wert als LONGLONG
type pdhItemLarge struct {
	Name    *uint16
	CStatus uint32
	_       uint32
	Large   int64
}

// gpuProcessVRAM liefert belegten dedizierten Grafikspeicher je PID in Bytes.
func gpuProcessVRAM() (map[uint32]uint64, error) {
	if err := pdhDLL.Load(); err != nil {
		return nil, fmt.Errorf("pdh.dll: %w", err)
	}
	var q windows.Handle
	if r, _, _ := procPdhOpenQueryW.Call(0, 0, uintptr(unsafe.Pointer(&q))); r != 0 {
		return nil, fmt.Errorf("PdhOpenQuery: 0x%x", r)
	}
	defer procPdhCloseQuery.Call(uintptr(q))
	path, _ := windows.UTF16PtrFromString(gpuProcCount)
	var c windows.Handle
	if r, _, _ := procPdhAddEnglishCounterW.Call(uintptr(q), uintptr(unsafe.Pointer(path)), 0, uintptr(unsafe.Pointer(&c))); r != 0 {
		return nil, fmt.Errorf("PdhAddEnglishCounter: 0x%x", r)
	}
	if r, _, _ := procPdhCollectQueryData.Call(uintptr(q)); r != 0 {
		return nil, fmt.Errorf("PdhCollectQueryData: 0x%x", r)
	}
	var size, n uint32
	r, _, _ := procPdhGetFormattedCounterAr.Call(uintptr(c), pdhFmtLarge, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&n)), 0)
	if r != pdhMoreData {
		if r == 0 { // no instances: no process is using the GPU
			return map[uint32]uint64{}, nil
		}
		return nil, fmt.Errorf("PdhGetFormattedCounterArray (size): 0x%x", r)
	}
	buf := make([]byte, size)
	if r, _, _ := procPdhGetFormattedCounterAr.Call(uintptr(c), pdhFmtLarge, uintptr(unsafe.Pointer(&size)), uintptr(unsafe.Pointer(&n)),
		uintptr(unsafe.Pointer(&buf[0]))); r != 0 {
		return nil, fmt.Errorf("PdhGetFormattedCounterArray: 0x%x", r)
	}
	out := make(map[uint32]uint64)
	items := unsafe.Slice((*pdhItemLarge)(unsafe.Pointer(&buf[0])), n)
	for _, it := range items {
		m := gpuInstPID.FindStringSubmatch(windows.UTF16PtrToString(it.Name))
		if m == nil || it.Large <= 0 {
			continue
		}
		pid, err := strconv.ParseUint(m[1], 10, 32)
		if err != nil {
			continue
		}
		out[uint32(pid)] += uint64(it.Large)
	}
	return out, nil
}

// jobProcessIDList corresponds to JOBOBJECT_BASIC_PROCESS_ID_LIST with space for 256 processes.
type jobProcessIDList struct {
	Assigned uint32
	InList   uint32
	List     [256]uintptr
}

// pids: all processes in the child's job - including grandchildren (the actual python.exe behind the venv starter, llama-server).
func (t *procTree) pids() []uint32 {
	if t == nil {
		return nil
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.job == 0 {
		return nil
	}
	var l jobProcessIDList
	const jobObjectBasicProcessIDList = 3
	if err := windows.QueryInformationJobObject(t.job, jobObjectBasicProcessIDList, uintptr(unsafe.Pointer(&l)),
		uint32(unsafe.Sizeof(l)), nil); err != nil {
		return nil
	}
	out := make([]uint32, 0, l.InList)
	for i := uint32(0); i < l.InList && i < uint32(len(l.List)); i++ {
		out = append(out, uint32(l.List[i]))
	}
	return out
}
