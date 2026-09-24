package main

import (
	"encoding/binary"
	"math"
	"strings"
	"testing"
	"unicode/utf16"
)

func putUTF16(b []byte, s string) {
	for i, c := range utf16.Encode([]rune(s)) {
		binary.LittleEndian.PutUint16(b[i*2:], c)
	}
}

// fakeGPUZ baut einen Shared-Memory-Block wie GPU-Z ihn schreibt.
func fakeGPUZ(sensors map[string]float64) []byte {
	b := make([]byte, gpuzSize)
	binary.LittleEndian.PutUint32(b[0:], 1)
	binary.LittleEndian.PutUint32(b[8:], 123456)
	off := gpuzHeaderSize
	putUTF16(b[off:], "CardName")
	putUTF16(b[off+512:], "NVIDIA GeForce RTX 5090")
	off += gpuzRecSize
	putUTF16(b[off:], "Monitor 1\\Serial")
	putUTF16(b[off+512:], "GEHEIM")
	off = gpuzHeaderSize + gpuzRecords*gpuzRecSize
	for name, v := range sensors {
		putUTF16(b[off:], name)
		putUTF16(b[off+512:], "x")
		binary.LittleEndian.PutUint32(b[off+528:], 1)
		binary.LittleEndian.PutUint64(b[off+532:], math.Float64bits(v))
		off += gpuzSensorSize
	}
	return b
}

func TestParseGPUZ(t *testing.T) {
	b := fakeGPUZ(map[string]float64{
		"GPU Clock": 457, "Memory Temperature": 44, "Hot Spot": 37.2, "GPU Voltage": 0.825,
		"16-Pin Power": 21.9, "16-Pin Voltage": 12.1, "Board Power Draw": 24.5, "PerfCap Reason": 5,
		"Crossbar Clock": math.NaN(), "CPU Temperature": 54,
	})
	d, err := parseGPUZ(b)
	if err != nil {
		t.Fatal(err)
	}
	if d.Version != 1 || d.LastUpdate != 123456 || d.Card != "NVIDIA GeForce RTX 5090" {
		t.Fatalf("Kopf falsch: %+v", d)
	}
	if len(d.Sensors) != 10 {
		t.Fatalf("Sensoren: %d", len(d.Sensors))
	}
	var s GPUSensors
	if !d.apply(&s) || !s.GPUZ {
		t.Fatal("apply lieferte nichts")
	}
	if *s.MemTempC != 44 || *s.HotSpotC != 37.2 || *s.GPUVoltageV != 0.825 || *s.Pin16PowerW != 21.9 || *s.Pin16VoltageV != 12.1 || *s.BoardPowerW != 24.5 || *s.CPUTempC != 54 {
		t.Fatalf("Werte falsch: %+v", s)
	}
	if *s.PerfCapMask != 5 || len(s.PerfCapReason) != 2 || s.PerfCapReason[0] != "power" || s.PerfCapReason[1] != "voltage_reliability" {
		t.Fatalf("PerfCap falsch: %v %v", *s.PerfCapMask, s.PerfCapReason)
	}
	if _, ok := d.value("Crossbar Clock"); ok {
		t.Fatal("NaN darf nicht als Wert gelten")
	}
	// Nichts aus den statischen Daten darf im Sensorblock landen
	st := map[string]any{}
	s.mqttState(st)
	for k, v := range st {
		if str, ok := v.(string); ok && str == "GEHEIM" {
			t.Fatalf("statischer Wert %s im MQTT-Zustand", k)
		}
	}
	if st["gpu_perfcap"] != "power, voltage_reliability" {
		t.Fatalf("gpu_perfcap: %v", st["gpu_perfcap"])
	}
}

func TestParseGPUZShort(t *testing.T) {
	if _, err := parseGPUZ(make([]byte, 100)); err == nil {
		t.Fatal("kurzer Puffer muss scheitern")
	}
}

func TestSensorsMQTTStateEmpty(t *testing.T) {
	st := map[string]any{}
	(*GPUSensors)(nil).mqttState(st)
	for _, k := range []string{"gpu_temp_c", "gpu_power_w", "gpu_mem_temp_c", "gpu_throttle", "gpu_perfcap"} {
		if v, ok := st[k]; !ok || v != nil {
			t.Fatalf("%s muss null sein, ist %v", k, v)
		}
	}
	th := uint64(0x24)
	s := &GPUSensors{TempC: iptr(60), ThrottleMask: &th, ThrottleReasons: decodeThrottle(th)}
	s.mqttState(st)
	if st["gpu_throttle"] != "sw_power_cap, sw_thermal" || !s.hasNVML() || s.hasGPUZ() {
		t.Fatalf("throttle: %v", st["gpu_throttle"])
	}
	if got := strings.Join(decodeThrottle(0x404), ","); got != "sw_power_cap,bit_0x400" {
		t.Fatalf("unbekanntes Bit: %s", got)
	}
	relay := &GPUSensors{MemTempC: fptr(61), GPUZ: true}
	s.mergeGPUZ(relay)
	if !s.hasGPUZ() || *s.MemTempC != 61 || s.TempC == nil {
		t.Fatalf("mergeGPUZ: %+v", s)
	}
}
