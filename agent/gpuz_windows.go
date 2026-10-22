//go:build windows

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// runGPUZRelay: `gpuz-relay` verb. Runs in the user's logon session (task "at logon", see
// Install-Service.ps1), reads the GPU-Z block, and sends the sensor whitelist to the service every 2 s (POST /gpuz on
// the health port, localhost only). Required because GPU-Z creates its object with a DACL for SYSTEM, Administrators, and the
// own logon session - the virtual service account `NT SERVICE\...` is not allowed to open it. If GPU-Z is not running,
// the relay waits silently (retry every 30 s, negligible CPU usage).
func runGPUZRelay(listen string) error {
	r := newGPUZReader(nil)
	cl := &http.Client{Timeout: 3 * time.Second}
	url := "http://" + listen + "/gpuz"
	fmt.Printf("gpuz-relay %s: GPU-Z -> %s every 2 s (Ctrl-C stops)\n", version, url)
	last := ""
	say := func(s string) {
		if s != last {
			fmt.Println(time.Now().Format("15:04:05"), "gpuz-relay:", s)
			last = s
		}
	}
	t := time.NewTicker(2 * time.Second)
	defer t.Stop()
	for ; ; <-t.C {
		d, err := r.read()
		if err != nil {
			say(err.Error())
			continue
		}
		var s GPUSensors
		if !d.apply(&s) {
			say("GPU-Z delivers none of the expected sensors")
			continue
		}
		b, _ := json.Marshal(&s)
		resp, err := cl.Post(url, "application/json", bytes.NewReader(b))
		if err != nil {
			say("service unreachable: " + err.Error())
			continue
		}
		resp.Body.Close()
		switch resp.StatusCode {
		case 200:
			say(fmt.Sprintf("delivering (%s, %d sensors)", d.Card, len(d.Sensors)))
		case 409:
			say("service has gpuz.enabled: false - relay waits")
		default:
			say(fmt.Sprintf("service answers HTTP %d", resp.StatusCode))
		}
	}
}

// gpuzReader reads the shared memory block from GPU-Z (see gpuz_parse.go). Two ways to access the object:
//   - OpenFileMapping("GPUZShMem"): if we run in the same logon session as GPU-Z (`gpu` verb, manual mode)
//   - NtOpenSection("\Sessions\<n>\BaseNamedObjects\GPUZShMem"): as a service in session 0 - GPU-Z creates the object in
//     its session without the Global\ prefix; LocalSystem can open it via the full NT path.
//
// If GPU-Z is not running, this is not an agent error: the block is simply missing, we retry every 30 s.
// If lastUpdate stops updating (GPU-Z exited or frozen), the block is considered stale and released.
type gpuzReader struct {
	log *Logger
	mu  sync.Mutex

	view    uintptr
	size    uintptr
	buf     []byte
	lastTry time.Time
	seen    bool // Log messages only on change
	stale   int
}

var (
	errGPUZAbsent = errors.New("GPU-Z is not running")
	errGPUZStale  = errors.New("GPU-Z values stale")

	kernel32           = windows.NewLazySystemDLL("kernel32.dll")
	pOpenFileMapping   = kernel32.NewProc("OpenFileMappingW")
	pMapViewOfFile     = kernel32.NewProc("MapViewOfFile")
	pUnmapViewOfFile   = kernel32.NewProc("UnmapViewOfFile")
	pGetTickCount64    = kernel32.NewProc("GetTickCount64")
	pActiveConsoleSess = kernel32.NewProc("WTSGetActiveConsoleSessionId")
	pProcIdToSession   = kernel32.NewProc("ProcessIdToSessionId")
	ntdll              = windows.NewLazySystemDLL("ntdll.dll")
	pNtOpenSection     = ntdll.NewProc("NtOpenSection")
)

const (
	fileMapRead        = 0x0004
	sectionMapRead     = 0x0004
	objCaseInsensitive = 0x40
	gpuzRetry          = 30 * time.Second
	gpuzStaleMs        = 15000
)

type ntUnicodeString struct {
	Length, MaximumLength uint16
	Buffer                *uint16
}

type ntObjectAttributes struct {
	Length                   uint32
	RootDirectory            uintptr
	ObjectName               *ntUnicodeString
	Attributes               uint32
	SecurityDescriptor       uintptr
	SecurityQualityOfService uintptr
}

func newGPUZReader(log *Logger) *gpuzReader {
	return &gpuzReader{log: log, buf: make([]byte, gpuzSize)}
}

func (r *gpuzReader) logf(level string, format string, a ...any) {
	if r.log == nil {
		return
	}
	if level == "warn" {
		r.log.Warnf(format, a...)
	} else {
		r.log.Infof(format, a...)
	}
}

// openSection opens a section object via its full NT path.
func openSection(path string) (windows.Handle, error) {
	u, err := windows.UTF16FromString(path)
	if err != nil {
		return 0, err
	}
	us := ntUnicodeString{Length: uint16((len(u) - 1) * 2), MaximumLength: uint16(len(u) * 2), Buffer: &u[0]}
	oa := ntObjectAttributes{ObjectName: &us, Attributes: objCaseInsensitive}
	oa.Length = uint32(unsafe.Sizeof(oa))
	var h windows.Handle
	rc, _, _ := pNtOpenSection.Call(uintptr(unsafe.Pointer(&h)), sectionMapRead, uintptr(unsafe.Pointer(&oa)))
	if rc != 0 {
		return 0, fmt.Errorf("NtOpenSection %s: 0x%08X", path, rc)
	}
	return h, nil
}

// candidates: object names in the order we try them.
func gpuzCandidates() []string {
	out := []string{"GPUZShMem"} // eigene Sitzung (OpenFileMapping)
	var own uint32
	if rc, _, _ := pProcIdToSession.Call(uintptr(windows.GetCurrentProcessId()), uintptr(unsafe.Pointer(&own))); rc == 0 {
		own = 0xFFFFFFFF
	}
	if act, _, _ := pActiveConsoleSess.Call(); uint32(act) != 0xFFFFFFFF && uint32(act) != own { // DWORD: upper 32 bits undefined
		out = append(out, fmt.Sprintf(`\Sessions\%d\BaseNamedObjects\GPUZShMem`, uint32(act)))
	}
	out = append(out, `\BaseNamedObjects\GPUZShMem`)
	return out
}

func (r *gpuzReader) open() error {
	var h windows.Handle
	var err error
	for i, name := range gpuzCandidates() {
		if i == 0 {
			hh, _, e := pOpenFileMapping.Call(fileMapRead, 0, uintptr(unsafe.Pointer(windows.StringToUTF16Ptr(name))))
			if hh == 0 {
				err = fmt.Errorf("OpenFileMapping: %v", e)
				continue
			}
			h = windows.Handle(hh)
		} else if h, err = openSection(name); err != nil {
			continue
		}
		view, _, e := pMapViewOfFile.Call(uintptr(h), fileMapRead, 0, 0, 0)
		windows.CloseHandle(h) // the view keeps the section alive
		if view == 0 {
			err = fmt.Errorf("MapViewOfFile: %v", e)
			continue
		}
		var mbi windows.MemoryBasicInformation
		if e := windows.VirtualQuery(view, &mbi, unsafe.Sizeof(mbi)); e != nil || mbi.RegionSize < gpuzSize {
			pUnmapViewOfFile.Call(view)
			err = fmt.Errorf("GPU-Z block too small (%d bytes)", mbi.RegionSize)
			continue
		}
		r.view, r.size = view, mbi.RegionSize
		return nil
	}
	return err
}

// viewPtr converts the address of a file mapping view into a pointer. The view is not in the Go heap, the GC does not
// move it; the indirection via a variable address prevents `go vet` (unsafeptr) from warning, which would be justified for
// heap addresses.
func viewPtr(u uintptr) unsafe.Pointer { return *(*unsafe.Pointer)(unsafe.Pointer(&u)) }

func (r *gpuzReader) drop() {
	if r.view != 0 {
		pUnmapViewOfFile.Call(r.view)
		r.view = 0
	}
}

// read returns the current block or errGPUZAbsent / errGPUZStale.
func (r *gpuzReader) read() (*gpuzData, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.view == 0 {
		if time.Since(r.lastTry) < gpuzRetry {
			return nil, errGPUZAbsent
		}
		r.lastTry = time.Now()
		if err := r.open(); err != nil {
			if r.seen {
				r.logf("info", "gpuz: block gone (%v) - sensors pause", err)
				r.seen = false
			}
			return nil, errGPUZAbsent
		}
	}
	src := unsafe.Slice((*byte)(viewPtr(r.view)), gpuzSize)
	copy(r.buf, src)
	d, err := parseGPUZ(r.buf)
	if err != nil {
		return nil, err
	}
	if d.Busy != 0 { // GPU-Z schreibt gerade: kurz warten, einmal neu lesen
		time.Sleep(5 * time.Millisecond)
		copy(r.buf, src)
		if d, err = parseGPUZ(r.buf); err != nil {
			return nil, err
		}
	}
	now, _, _ := pGetTickCount64.Call()
	age := int64(int32(uint32(now) - d.LastUpdate)) // both 32 bits, overflow cancels out; signed
	if age > gpuzStaleMs || age < -gpuzStaleMs {
		r.stale++
		if r.stale >= 2 { // two consecutive measurements stale: GPU-Z is gone or hanging
			r.drop()
			r.stale = 0
			if r.seen {
				r.logf("info", "gpuz: no fresh values for %d ms - block released", age)
				r.seen = false
			}
		}
		return nil, errGPUZStale
	}
	r.stale = 0
	if !r.seen {
		r.logf("info", "gpuz: sensors found (%s, %d sensors, version %d)", d.Card, len(d.Sensors), d.Version)
		r.seen = true
	}
	return d, nil
}
