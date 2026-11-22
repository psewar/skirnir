package main

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// Agent 0.36.0 (ADR-0102, ADR-0103): a node without an NVIDIA GPU runs, sends its heartbeat and says so as a fact; game
// detection and the language of the Home Assistant names are settings.

func testLogger(t *testing.T) *Logger {
	t.Helper()
	log, err := newLogger(LogCfg{Dir: t.TempDir()}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() })
	return log
}

func TestGPUProbeNone(t *testing.T) {
	g, err := newGPU(gpuProbeNone)
	if err != nil || g == nil {
		t.Fatalf("gpu.probe none must not fail: %v", err)
	}
	if g.Present() || g.Source() != gpuSourceNone {
		t.Fatalf("present=%v source=%q, want false/none", g.Present(), g.Source())
	}
	if s, err := g.Sample(context.Background()); s != nil || !errors.Is(err, errNoGPU) {
		t.Fatalf("Sample without a GPU: %v %v", s, err)
	}
	if s, err := g.Last(); s != nil || err != nil {
		t.Fatalf("Last without a GPU: %v %v", s, err)
	}
	if lim := g.PowerLimits(); lim.OK {
		t.Fatal("power limits without a GPU must not be OK")
	}
	if err := g.SetPowerLimit(300); !errors.Is(err, errNoGPU) {
		t.Fatalf("SetPowerLimit without a GPU: %v", err)
	}
}

func TestHeartbeatWithoutGPU(t *testing.T) {
	log := testLogger(t)
	g, _ := newGPU(gpuProbeNone)
	h := newHeartbeat("cpu-node", g, log)
	h.guard = newGuard(GuardCfg{}, g, log)
	h.game = newGameWatch(log, false)
	p, err := h.payload(context.Background())
	if err != nil {
		t.Fatalf("heartbeat without a GPU must go out: %v", err)
	}
	if p["gpu_present"] != false {
		t.Fatalf("gpu_present = %v, want false", p["gpu_present"])
	}
	for _, k := range []string{"gpu_util_pct", "vram_total_mib", "vram_used_mib", "vram_free_mib", "sensors"} {
		if _, ok := p[k]; ok {
			t.Errorf("heartbeat without a GPU carries %s", k)
		}
	}
	if _, ok := memoryFact(); ok {
		if _, ok := p["memory"]; !ok {
			t.Error("heartbeat without a GPU lost the memory fact the router sizes models by")
		}
	}
	gs, ok := p["gpu_guard"].(GuardStatus)
	if !ok || gs.State != guardNotApplicable || gs.Problem || gs.Reason != guardReasonNoGPU {
		t.Fatalf("guard status without a GPU: %+v", p["gpu_guard"])
	}
	b, _ := json.Marshal(p)
	if !strings.Contains(string(b), `"gpu_present":false`) {
		t.Fatalf("wire form without gpu_present false: %s", b)
	}
}

func TestGuardWithoutGPUIsNoProblem(t *testing.T) {
	log := testLogger(t)
	g, _ := newGPU(gpuProbeNone)
	off := false
	for _, cfg := range []GuardCfg{{}, {Enabled: &off}} {
		st := newGuard(cfg, g, log).Status()
		if st.State != guardNotApplicable || st.Problem {
			t.Fatalf("guard without a GPU (cfg %+v): %+v - want not_applicable without problem", cfg, st)
		}
	}
	// Run returns at once instead of ticking without a GPU.
	gd := newGuard(GuardCfg{}, g, log)
	done := make(chan struct{})
	go func() { gd.Run(context.Background()); close(done) }()
	<-done
}

func TestFactsWithoutGPU(t *testing.T) {
	g, _ := newGPU(gpuProbeNone)
	f := collectFacts(context.Background(), "", "http://127.0.0.1:1", upstreamOpenAI, g)
	if f.GPUPresent || f.GPU != "" || f.VRAMTotalMiB != 0 {
		t.Fatalf("facts without a GPU: present=%v gpu=%q vram=%d", f.GPUPresent, f.GPU, f.VRAMTotalMiB)
	}
	b, _ := json.Marshal(f)
	if !strings.Contains(string(b), `"gpu_present":false`) {
		t.Fatalf("facts must state gpu_present false explicitly: %s", b)
	}
}

func TestConfigGPUProbe(t *testing.T) {
	c, err := loadConfig(writeCfg(t, "router:\n  url: https://router.example.net:11435\n"))
	if err != nil {
		t.Fatal(err)
	}
	if c.GPU.Probe != gpuProbeAuto {
		t.Fatalf("default gpu.probe = %q, want auto", c.GPU.Probe)
	}
	for _, v := range []string{gpuProbeAuto, gpuProbeNvidia, gpuProbeNone} {
		if _, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\ngpu:\n  probe: "+v+"\n")); err != nil {
			t.Fatalf("gpu.probe %s rejected: %v", v, err)
		}
	}
	if _, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\ngpu:\n  probe: amd\n")); err == nil {
		t.Fatal("gpu.probe amd must be rejected")
	}
}

func TestConfigGameEnabled(t *testing.T) {
	c, err := loadConfig(writeCfg(t, "router:\n  url: https://router.example.net:11435\n"))
	if err != nil {
		t.Fatal(err)
	}
	if c.Game.on() != gameScanSupported {
		t.Fatalf("default game.enabled = %v, want %v (on where the platform detects games)", c.Game.on(), gameScanSupported)
	}
	c, err = loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\ngame:\n  enabled: false\n"))
	if err != nil || c.Game.on() {
		t.Fatalf("game.enabled false: on=%v err=%v", c != nil && c.Game.on(), err)
	}
	gw := newGameWatch(testLogger(t), false)
	if f := gw.Fact(); f.Enabled || f.Running {
		t.Fatalf("disabled game watch reports %+v", f)
	}
	done := make(chan struct{})
	go func() { gw.Run(context.Background()); close(done) }()
	<-done // returns at once when switched off (or unsupported)
}

func TestConfigEntityLanguage(t *testing.T) {
	for _, v := range []string{"de", "en"} {
		if _, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\nmqtt:\n  entity_language: "+v+"\n")); err != nil {
			t.Fatalf("mqtt.entity_language %s rejected: %v", v, err)
		}
	}
	if _, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\nmqtt:\n  entity_language: fr\n")); err == nil {
		t.Fatal("mqtt.entity_language fr must be rejected")
	}
}

func TestEntityNamesEnglishComplete(t *testing.T) {
	for _, s := range sensorDefs {
		if _, ok := entityNamesEN[s.objID]; !ok {
			t.Errorf("no English name for %s", s.objID)
		}
		if s.displayName("") != s.name || s.displayName(entityLanguageDE) != s.name {
			t.Errorf("%s: German stays the default name", s.objID)
		}
	}
}

func TestDiscoveryEnglishKeepsIDs(t *testing.T) {
	log := testLogger(t)
	for _, lang := range []string{"", entityLanguageEN} {
		m := newMQTT(MQTTCfg{DeviceID: "testnode", DeviceName: "Testnode", DiscoveryPrefix: "homeassistant", EntityLanguage: lang},
			SecretStoreCfg{}, "testnode", log, nil, nil, nil, nil)
		c := &fakeClient{}
		m.syncDiscovery(c)
		cfg, _ := c.last("router_heartbeat")
		want := map[string]string{"": `"name":"Router-Heartbeat"`, entityLanguageEN: `"name":"Router heartbeat"`}[lang]
		for _, part := range []string{want, `"unique_id":"testnode_router_heartbeat"`, `"default_entity_id":"binary_sensor.testnode_router_heartbeat"`} {
			if !strings.Contains(cfg, part) {
				t.Fatalf("language %q: discovery without %s: %s", lang, part, cfg)
			}
		}
	}
}

func TestMQTTGPUGroupsOffWithoutGPU(t *testing.T) {
	log := testLogger(t)
	g, _ := newGPU(gpuProbeNone)
	m := newMQTT(MQTTCfg{DeviceID: "testnode", DiscoveryPrefix: "homeassistant"}, SecretStoreCfg{}, "testnode", log, g, nil, nil, nil)
	m.sensorState(map[string]any{})
	for _, grp := range []string{"gpu", "gpuz"} {
		yes, known := m.applies[grp]
		if !known || yes {
			t.Fatalf("group %s without a GPU: applies=%v known=%v, want decided false", grp, yes, known)
		}
	}
}
