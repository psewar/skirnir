package main

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const giB = uint64(1) << 30

func TestUpstreamMemoFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, upstreamMemoFile)
	m, err := loadUpstreamMemo(path)
	if err != nil || m.known() || m.NeedBytes != 0 {
		t.Fatalf("no file yet: empty memo, no error: %+v %v", m, err)
	}
	want := upstreamMemo{Tags: []json.RawMessage{json.RawMessage(`{"name":"big-moe"}`)}, NeedBytes: 42 * giB}
	if err := saveUpstreamMemo(path, want); err != nil {
		t.Fatal(err)
	}
	got, err := loadUpstreamMemo(path)
	if err != nil || !got.known() || got.NeedBytes != want.NeedBytes || string(got.Tags[0]) != `{"name":"big-moe"}` {
		t.Fatalf("round trip: %+v %v", got, err)
	}
	if _, err := os.Stat(path + ".tmp"); err == nil {
		t.Fatal("temp file left behind")
	}
	if err := os.WriteFile(path, []byte("{broken"), 0o600); err != nil {
		t.Fatal(err)
	}
	if m, err := loadUpstreamMemo(path); err == nil || m.known() {
		t.Fatalf("broken file: reported as error and treated as none: %+v %v", m, err)
	}
}

// heldSetup: an upstream "strata" with a standby "ollama", as on the desktop node.
func heldSetup(t *testing.T, standbyURL string) (*Supervisor, *upstreamSwitch, *OllamaWatcher, *upstreamHold) {
	t.Helper()
	sup := &Supervisor{children: map[string]*childState{"strata": {}, "ollama": {}}}
	sup.Park("ollama")
	pct := 90
	fbCfg := &FallbackCfg{Child: "ollama", Upstream: standbyURL, MaxRAMPercent: &pct}
	sw := newUpstreamSwitch(fbCfg)
	w := newOllamaWatcher("http://127.0.0.1:9", upstreamOpenAI, quietLogger())
	return sup, sw, w, newUpstreamHold(sup, "strata", fbCfg, sw, w, quietLogger())
}

// With remembered models the agent starts with the upstream held and the standby serving, and still lists the upstream's
// models so the router can release it; without them the upstream starts as before.
func TestStartHeld(t *testing.T) {
	fb := &fakeOllama{}
	fb.set(`{"name":"small:8b","size_vram":1}`, `{"name":"small:8b"}`, false)
	fbSrv := httptest.NewServer(fb)
	defer fbSrv.Close()

	dir := t.TempDir()
	sup, sw, w, u := heldSetup(t, fbSrv.URL)
	u.remember(dir)
	if u.startHeld(time.Now()) || sup.isHeld("strata") || !sup.isHeld("ollama") {
		t.Fatalf("nothing remembered: upstream starts as before: strata held=%v ollama held=%v", sup.isHeld("strata"), sup.isHeld("ollama"))
	}

	memo := upstreamMemo{Tags: []json.RawMessage{json.RawMessage(`{"name":"big-moe","digest":"openai:big-moe"}`)}, NeedBytes: 42 * giB}
	if err := saveUpstreamMemo(filepath.Join(dir, upstreamMemoFile), memo); err != nil {
		t.Fatal(err)
	}
	sup, sw, w, u = heldSetup(t, fbSrv.URL)
	u.remember(dir)
	now := time.Now()
	if !u.startHeld(now) {
		t.Fatal("remembered models: start held")
	}
	if !sup.isHeld("strata") || sup.isHeld("ollama") || !u.held || !u.fbRunning {
		t.Fatalf("held start: upstream parked, standby released: strata=%v ollama=%v", sup.isHeld("strata"), sup.isHeld("ollama"))
	}
	st, _ := w.Snapshot()
	if !st.Up || st.UpstreamState != upstreamHeld || !strings.Contains(string(joinRaw(st.Tags)), "big-moe") {
		t.Fatalf("held start reported at once with the remembered models: %+v", st)
	}
	w.poll(context.Background(), now.Add(time.Second))
	st, _ = w.Snapshot()
	tags := string(joinRaw(st.Tags))
	if st.Serving != servingOllama || !strings.Contains(tags, "small:8b") || !strings.Contains(tags, "big-moe") {
		t.Fatalf("standby answering: its models plus the upstream's: %+v tags=%s", st, tags)
	}
	if target, done := sw.acquire("http://127.0.0.1:8080"); target != fbSrv.URL {
		t.Fatalf("held start: requests go to the standby, got %s", target)
	} else {
		done()
	}

	noStandby := newUpstreamHold(&Supervisor{children: map[string]*childState{"strata": {}}}, "strata", nil, nil, w, quietLogger())
	noStandby.remember(dir)
	if noStandby.startHeld(now) {
		t.Fatal("without a standby a held start would leave the node with nothing to serve")
	}
}

// A release that would take the RAM over max_ram_percent is refused: the upstream stays held, the standby keeps serving,
// the reason is reported; once there is room again the block is cleared and the next release goes through.
func TestReleaseRAMCheck(t *testing.T) {
	sup, sw, w, u := heldSetup(t, "http://127.0.0.1:11434")
	u.remember(t.TempDir())
	u.memo = upstreamMemo{Tags: []json.RawMessage{json.RawMessage(`{"name":"big-moe"}`)}, NeedBytes: 42 * giB}
	avail := 20.0
	u.mem = func() (MemFact, bool) { return MemFact{RAMAvailableGiB: avail, RAMTotalGiB: 64}, true }
	now := time.Now()
	u.apply(true, now)
	if !u.held || !u.fbRunning {
		t.Fatal("setup: held with the standby")
	}

	u.apply(false, now) // in use 44 + need 42 = 86 GiB > 90 % of 64 = 57.6
	st, _ := w.Snapshot()
	if !u.held || !sup.isHeld("strata") || !u.fbRunning || st.UpstreamState != upstreamHeld {
		t.Fatalf("refused release: upstream stays held, standby serves: held=%v %+v", u.held, st)
	}
	b := st.UpstreamBlocked
	if b == nil || b.Reason != blockedMemory || b.NeedGiB != 42 || b.RAMAvailableGiB != 20 || b.LimitPercent != 90 {
		t.Fatalf("reason reported with its numbers: %+v", b)
	}
	if target, done := sw.acquire("http://127.0.0.1:8080"); target != "http://127.0.0.1:11434" {
		t.Fatalf("refused release: requests still on the standby, got %s", target)
	} else {
		done()
	}
	rev := st.Rev
	u.apply(false, now)
	if st, _ = w.Snapshot(); st.Rev != rev {
		t.Fatalf("same refusal again: no new revision (%d -> %d)", rev, st.Rev)
	}

	avail = 50 // in use 14 + 42 = 56 <= 57.6
	u.sample()
	if st, _ = w.Snapshot(); st.UpstreamBlocked != nil || u.blocked != nil {
		t.Fatalf("room again: block cleared by the sampler: %+v", st.UpstreamBlocked)
	}
	u.apply(false, now)
	if u.held || sup.isHeld("strata") || u.fbRunning {
		t.Fatalf("release with room: upstream started, standby stopped: held=%v", u.held)
	}

	u.memo.NeedBytes = 0 // need not learned yet: nothing to compare, the release goes through
	if u.ramBlock() != nil {
		t.Fatal("unknown need blocks nothing")
	}
	u.memo.NeedBytes, u.maxRAMPct = 42*giB, 0
	avail = 1
	if u.ramBlock() != nil {
		t.Fatal("max_ram_percent 0 = no check")
	}
}

// While the upstream runs, the sampler remembers its models and the peak of its private bytes; at the hold the run's peak
// becomes the remembered need, so a smaller setup lowers it again.
func TestSampleLearnsUpstream(t *testing.T) {
	up := &fakeOpenAI{}
	up.set(`{"id":"big-moe","status":{"value":"loaded"}}`)
	upSrv := httptest.NewServer(up)
	defer upSrv.Close()

	dir := t.TempDir()
	sup := &Supervisor{children: map[string]*childState{"strata": {}, "ollama": {}}}
	sup.Park("ollama")
	pct := 90
	fbCfg := &FallbackCfg{Child: "ollama", Upstream: "http://127.0.0.1:11434", MaxRAMPercent: &pct}
	w := newOllamaWatcher(upSrv.URL, upstreamOpenAI, quietLogger())
	u := newUpstreamHold(sup, "strata", fbCfg, newUpstreamSwitch(fbCfg), w, quietLogger())
	u.remember(dir)
	priv := uint64(30) * giB
	u.private = func() (uint64, bool) { return priv, true }
	u.mem = func() (MemFact, bool) { return MemFact{RAMAvailableGiB: 60, RAMTotalGiB: 64}, true } // room for the release

	w.poll(context.Background(), time.Now())
	u.sample()
	priv = 42 * giB
	u.sample()
	m, err := loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile))
	if err != nil || !m.known() || m.NeedBytes != 42*giB || !strings.Contains(string(m.Tags[0]), "big-moe") {
		t.Fatalf("learned while running: models and peak need saved: %+v %v", m, err)
	}
	tags, _ := w.UpstreamTags()
	if !sameRaw(tags, m.Tags) {
		t.Fatalf("saved models byte-equal to the watcher's, else every sample rewrites the file: %s vs %s", joinRaw(tags), joinRaw(m.Tags))
	}

	u.apply(true, time.Now()) // end of this run: peak 42 stays
	u.apply(false, time.Now())
	priv = 35 * giB // next run is smaller
	u.sample()
	u.apply(true, time.Now())
	if m, _ = loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile)); m.NeedBytes != 35*giB {
		t.Fatalf("the last run's peak becomes the need: %d GiB", m.NeedBytes/giB)
	}
}

// 0.26.1: the router holds the server seconds after it answers, before any sample - the hold itself must remember the
// models and the need, or the next start learns again.
func TestHoldLearnsWithoutSample(t *testing.T) {
	up := &fakeOpenAI{}
	up.set(`{"id":"big-moe","status":{"value":"loaded"}}`)
	upSrv := httptest.NewServer(up)
	defer upSrv.Close()

	dir := t.TempDir()
	sup := &Supervisor{children: map[string]*childState{"strata": {}, "ollama": {}}}
	sup.Park("ollama")
	pct := 90
	fbCfg := &FallbackCfg{Child: "ollama", Upstream: "http://127.0.0.1:11434", MaxRAMPercent: &pct}
	w := newOllamaWatcher(upSrv.URL, upstreamOpenAI, quietLogger())
	u := newUpstreamHold(sup, "strata", fbCfg, newUpstreamSwitch(fbCfg), w, quietLogger())
	u.remember(dir)
	if u.startHeld(time.Now()) {
		t.Fatal("setup: nothing remembered, the server starts")
	}
	u.private = func() (uint64, bool) { return 41 * giB, true }
	w.poll(context.Background(), time.Now()) // the server answers; no sample has run
	u.apply(true, time.Now())                // the router holds it at once
	m, err := loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile))
	if err != nil || !m.known() || m.NeedBytes != 41*giB {
		t.Fatalf("the hold remembered models and need: %+v %v", m, err)
	}
	sup2 := &Supervisor{children: map[string]*childState{"strata": {}, "ollama": {}}}
	sup2.Park("ollama")
	u2 := newUpstreamHold(sup2, "strata", fbCfg, newUpstreamSwitch(fbCfg), newOllamaWatcher(upSrv.URL, upstreamOpenAI, quietLogger()), quietLogger())
	u2.remember(dir)
	if !u2.startHeld(time.Now()) || !sup2.isHeld("strata") {
		t.Fatal("next start: held")
	}
}

func TestConfigMaxRAMPercent(t *testing.T) {
	base := "router:\n  url: https://r.example.net\nchildren:\n  - name: strata\n    cmd: strata.exe\n" +
		"  - name: ollama\n    cmd: C:/Users/x/AppData/Local/Programs/Ollama/ollama.exe\n    args: [serve]\n" +
		"tunnel:\n  upstream: http://127.0.0.1:8080\n  upstream_api: openai\n  upstream_child: strata\n  upstream_fallback:\n    child: ollama\n"
	c, err := loadConfig(writeCfg(t, base))
	if err != nil || c.Tunnel.UpstreamFallback.MaxRAMPercent == nil || *c.Tunnel.UpstreamFallback.MaxRAMPercent != 90 {
		t.Fatalf("default 90: %v %v", c, err)
	}
	if c, err := loadConfig(writeCfg(t, base+"    max_ram_percent: 0\n")); err != nil || *c.Tunnel.UpstreamFallback.MaxRAMPercent != 0 {
		t.Fatalf("0 = no check, accepted: %v", err)
	}
	for _, bad := range []string{"40", "100"} {
		if _, err := loadConfig(writeCfg(t, base+"    max_ram_percent: "+bad+"\n")); err == nil {
			t.Fatalf("max_ram_percent %s accepted", bad)
		}
	}
}
