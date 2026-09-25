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

// runGPUZRelay: Verb `gpuz-relay`. Laeuft in der Anmeldesitzung des Benutzers (Aufgabe "bei Anmeldung", siehe
// Install-Service.ps1), liest den GPU-Z-Block und schickt die Sensor-Whitelist alle 2 s an den Dienst (POST /gpuz auf
// dem Health-Port, nur localhost). Noetig, weil GPU-Z sein Objekt mit einer DACL fuer SYSTEM, Administratoren und die
// eigene Anmeldesitzung anlegt - das virtuelle Dienstkonto `NT SERVICE\...` darf es nicht oeffnen. Laeuft GPU-Z nicht,
// wartet das Relay still (Versuch alle 30 s, praktisch kein CPU-Verbrauch).
func runGPUZRelay(listen string) error {
	r := newGPUZReader(nil)
	cl := &http.Client{Timeout: 3 * time.Second}
	url := "http://" + listen + "/gpuz"
	fmt.Printf("gpuz-relay %s: GPU-Z -> %s alle 2 s (Ctrl-C beendet)\n", version, url)
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
			say("GPU-Z liefert keinen der erwarteten Sensoren")
			continue
		}
		b, _ := json.Marshal(&s)
		resp, err := cl.Post(url, "application/json", bytes.NewReader(b))
		if err != nil {
			say("Dienst nicht erreichbar: " + err.Error())
			continue
		}
		resp.Body.Close()
		switch resp.StatusCode {
		case 200:
			say(fmt.Sprintf("liefert (%s, %d Sensoren)", d.Card, len(d.Sensors)))
		case 409:
			say("Dienst hat gpuz.enabled: false - Relay wartet")
		default:
			say(fmt.Sprintf("Dienst antwortet HTTP %d", resp.StatusCode))
		}
	}
}

// gpuzReader liest den Shared-Memory-Block von GPU-Z (siehe gpuz_parse.go). Zwei Wege zum Objekt:
//   - OpenFileMapping("GPUZShMem"): wenn wir in derselben Anmeldesitzung laufen wie GPU-Z (Verb `gpu`, Handbetrieb)
//   - NtOpenSection("\Sessions\<n>\BaseNamedObjects\GPUZShMem"): als Dienst in Sitzung 0 - GPU-Z legt das Objekt in
//     seiner Sitzung an, ohne Global\-Praefix; LocalSystem darf es ueber den vollen NT-Pfad oeffnen.
//
// Laeuft GPU-Z nicht, ist das kein Fehler des Agenten: der Block fehlt einfach, wir probieren es alle 30 s wieder.
// Bleibt lastUpdate stehen (GPU-Z beendet oder eingefroren), gilt der Block als veraltet und wird losgelassen.
type gpuzReader struct {
	log *Logger
	mu  sync.Mutex

	view    uintptr
	size    uintptr
	buf     []byte
	lastTry time.Time
	seen    bool // Log-Meldungen nur bei Wechsel
	stale   int
}

var (
	errGPUZAbsent = errors.New("GPU-Z laeuft nicht")
	errGPUZStale  = errors.New("GPU-Z-Werte veraltet")

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

// openSection oeffnet ein Section-Objekt ueber seinen vollen NT-Pfad.
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

// candidates: Objektnamen in der Reihenfolge, in der wir sie probieren.
func gpuzCandidates() []string {
	out := []string{"GPUZShMem"} // eigene Sitzung (OpenFileMapping)
	var own uint32
	if rc, _, _ := pProcIdToSession.Call(uintptr(windows.GetCurrentProcessId()), uintptr(unsafe.Pointer(&own))); rc == 0 {
		own = 0xFFFFFFFF
	}
	if act, _, _ := pActiveConsoleSess.Call(); uint32(act) != 0xFFFFFFFF && uint32(act) != own { // DWORD: obere 32 Bit nicht definiert
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
		windows.CloseHandle(h) // die View haelt die Section am Leben
		if view == 0 {
			err = fmt.Errorf("MapViewOfFile: %v", e)
			continue
		}
		var mbi windows.MemoryBasicInformation
		if e := windows.VirtualQuery(view, &mbi, unsafe.Sizeof(mbi)); e != nil || mbi.RegionSize < gpuzSize {
			pUnmapViewOfFile.Call(view)
			err = fmt.Errorf("GPU-Z-Block zu klein (%d Bytes)", mbi.RegionSize)
			continue
		}
		r.view, r.size = view, mbi.RegionSize
		return nil
	}
	return err
}

// viewPtr macht aus der Adresse einer File-Mapping-View einen Pointer. Die View liegt nicht im Go-Heap, der GC bewegt
// sie nicht; der Umweg ueber die Adresse einer Variablen haelt `go vet` (unsafeptr) von der Warnung ab, die fuer
// Heap-Adressen berechtigt waere.
func viewPtr(u uintptr) unsafe.Pointer { return *(*unsafe.Pointer)(unsafe.Pointer(&u)) }

func (r *gpuzReader) drop() {
	if r.view != 0 {
		pUnmapViewOfFile.Call(r.view)
		r.view = 0
	}
}

// read liefert den aktuellen Block oder errGPUZAbsent / errGPUZStale.
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
				r.logf("info", "gpuz: Block nicht mehr da (%v) - Sensoren pausieren", err)
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
	age := int64(int32(uint32(now) - d.LastUpdate)) // beide 32 Bit, Ueberlauf hebt sich auf; vorzeichenbehaftet
	if age > gpuzStaleMs || age < -gpuzStaleMs {
		r.stale++
		if r.stale >= 2 { // zwei Messungen hintereinander alt: GPU-Z ist weg oder haengt
			r.drop()
			r.stale = 0
			if r.seen {
				r.logf("info", "gpuz: keine frischen Werte seit %d ms - Block losgelassen", age)
				r.seen = false
			}
		}
		return nil, errGPUZStale
	}
	r.stale = 0
	if !r.seen {
		r.logf("info", "gpuz: Sensoren gefunden (%s, %d Sensoren, Version %d)", d.Card, len(d.Sensors), d.Version)
		r.seen = true
	}
	return d, nil
}
