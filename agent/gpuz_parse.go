package main

import (
	"encoding/binary"
	"errors"
	"math"
	"unicode/utf16"
)

// GPU-Z legt, solange es laeuft, einen Shared-Memory-Block "GPUZShMem" an (Sensor-Schnittstelle fuer Fremdprogramme).
// Aufbau (gepackt, keine Fuellbytes; gemessen 200204 Bytes, Mapping auf 200704 aufgerundet):
//
//	uint32 version; int32 busy; uint32 lastUpdate (GetTickCount ms)
//	128 x { wchar key[256]; wchar value[256] }                 statische Kartendaten (Name, BIOS, Treiber, Monitor ...)
//	128 x { wchar name[256]; wchar unit[8]; uint32 digits; double value }   Sensoren
//
// Wir uebernehmen nur eine Whitelist von Sensoren; die statischen Daten (darunter Monitor-Seriennummern) bleiben liegen.
const (
	gpuzRecords    = 128
	gpuzHeaderSize = 12
	gpuzRecSize    = 256*2 + 256*2
	gpuzSensorSize = 256*2 + 8*2 + 4 + 8
	gpuzSize       = gpuzHeaderSize + gpuzRecords*gpuzRecSize + gpuzRecords*gpuzSensorSize // 200204
)

type gpuzSensor struct {
	Name, Unit string
	Digits     uint32
	Value      float64
}

type gpuzData struct {
	Version    uint32
	Busy       int32
	LastUpdate uint32 // GetTickCount() beim letzten Schreiben
	Card       string // CardName aus den statischen Daten (nur fuers Log)
	Sensors    []gpuzSensor
}

var errGPUZShort = errors.New("GPU-Z Shared Memory zu kurz")

// utf16z dekodiert einen NUL-terminierten UTF-16LE-Puffer.
func utf16z(b []byte) string {
	u := make([]uint16, 0, len(b)/2)
	for i := 0; i+1 < len(b); i += 2 {
		c := binary.LittleEndian.Uint16(b[i:])
		if c == 0 {
			break
		}
		u = append(u, c)
	}
	return string(utf16.Decode(u))
}

func parseGPUZ(b []byte) (*gpuzData, error) {
	if len(b) < gpuzSize {
		return nil, errGPUZShort
	}
	d := &gpuzData{
		Version:    binary.LittleEndian.Uint32(b[0:]),
		Busy:       int32(binary.LittleEndian.Uint32(b[4:])),
		LastUpdate: binary.LittleEndian.Uint32(b[8:]),
	}
	off := gpuzHeaderSize
	for i := 0; i < gpuzRecords; i++ {
		key := utf16z(b[off : off+512])
		if key == "CardName" {
			d.Card = utf16z(b[off+512 : off+1024])
		}
		off += gpuzRecSize
	}
	for i := 0; i < gpuzRecords; i++ {
		name := utf16z(b[off : off+512])
		if name != "" {
			d.Sensors = append(d.Sensors, gpuzSensor{
				Name:   name,
				Unit:   utf16z(b[off+512 : off+528]),
				Digits: binary.LittleEndian.Uint32(b[off+528:]),
				Value:  math.Float64frombits(binary.LittleEndian.Uint64(b[off+532:])),
			})
		}
		off += gpuzSensorSize
	}
	return d, nil
}

// value liefert einen Sensor nach Namen; NaN (GPU-Z zeigt "-") gilt als nicht vorhanden.
func (d *gpuzData) value(name string) (float64, bool) {
	for _, s := range d.Sensors {
		if s.Name == name {
			if math.IsNaN(s.Value) || math.IsInf(s.Value, 0) {
				return 0, false
			}
			return s.Value, true
		}
	}
	return 0, false
}

// apply uebertraegt die Whitelist in den Sensorblock. Rueckgabe: ob mindestens ein Wert kam.
func (d *gpuzData) apply(s *GPUSensors) bool {
	any := false
	set := func(name string, dst **float64) {
		if v, ok := d.value(name); ok {
			*dst = fptr(v)
			any = true
		}
	}
	set("Memory Temperature", &s.MemTempC)
	set("Hot Spot", &s.HotSpotC)
	set("GPU Voltage", &s.GPUVoltageV)
	set("16-Pin Power", &s.Pin16PowerW)
	set("16-Pin Voltage", &s.Pin16VoltageV)
	set("Board Power Draw", &s.BoardPowerW)
	set("CPU Temperature", &s.CPUTempC)
	if v, ok := d.value("PerfCap Reason"); ok {
		m := int(v)
		s.PerfCapMask, s.PerfCapReason = iptr(m), decodePerfCap(m)
		any = true
	}
	s.GPUZ = any
	return any
}
