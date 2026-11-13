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

// noSettle: the hold reads the released RAM a second time after releaseSettle; the tests' RAM is a function, no need to wait.
func noSettle(t *testing.T) {
	old := releaseSettle
	releaseSettle = 0
	t.Cleanup(func() { releaseSettle = old })
}

// withMargin: the need from a measured peak (needMarginPercent).
func withMargin(peak uint64) uint64 { return peak + peak*needMarginPercent/100 }

// ADR-0092: while the upstream runs, the sampler remembers its models and the peak of its resident memory (not its
// commit); the need is that peak plus the margin and only grows - a later, shorter run does not lower it.
func TestSampleLearnsUpstream(t *testing.T) {
	noSettle(t)
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
	run := procMem{Resident: 20 * giB, Commit: 52 * giB} // commit far above what is resident, as with a RAM budget
	u.measure = func() (procMem, bool) { return run, true }
	u.mem = func() (MemFact, bool) { return MemFact{RAMAvailableGiB: 60, RAMTotalGiB: 64}, true } // room for the release

	w.poll(context.Background(), time.Now())
	u.sample()
	run.Resident = 28 * giB // pages come in while it runs
	u.sample()
	m, err := loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile))
	if err != nil || !m.known() || !strings.Contains(string(m.Tags[0]), "big-moe") {
		t.Fatalf("learned while running: models saved: %+v %v", m, err)
	}
	if m.NeedBytes != withMargin(28*giB) || m.PeakResidentBytes != 28*giB || m.PeakCommitBytes != 52*giB ||
		m.Measure != needMeasure || m.Config != u.config {
		t.Fatalf("need = resident peak + margin, commit only for comparison: %+v", m)
	}
	tags, _ := w.UpstreamTags()
	if !sameRaw(tags, m.Tags) {
		t.Fatalf("saved models byte-equal to the watcher's, else every sample rewrites the file: %s vs %s", joinRaw(tags), joinRaw(m.Tags))
	}

	run.Resident += needSaveStep / 2 // a small rise: kept in memory, not written at once
	u.sample()
	if m, _ = loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile)); m.PeakResidentBytes != 28*giB {
		t.Fatalf("a rise below needSaveStep is not written by the sampler: %d", m.PeakResidentBytes)
	}
	if u.memo.PeakResidentBytes != 28*giB+needSaveStep/2 {
		t.Fatalf("but it counts at once: %d", u.memo.PeakResidentBytes)
	}

	u.apply(true, time.Now()) // end of this run: the hold writes the exact values
	if m, _ = loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile)); m.PeakResidentBytes != 28*giB+needSaveStep/2 {
		t.Fatalf("the hold writes the exact peak: %d", m.PeakResidentBytes)
	}
	want := m.NeedBytes
	u.apply(false, time.Now())
	run.Resident = 10 * giB // next run is short: the router held it before its pages came in
	u.sample()
	u.apply(true, time.Now())
	if m, _ = loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile)); m.NeedBytes != want {
		t.Fatalf("a shorter run does not lower the need: %.1f GiB, want %.1f", gib(m.NeedBytes), gib(want))
	}
}

// ADR-0092: the RAM that becomes available when the server stops counts too - pages a driver holds for it outside its
// working set (pinned for the GPU) show only there. Also 0.26.1: the router holds the server seconds after it answers,
// before any sample - the hold itself must remember the models and the need, or the next start learns again.
func TestHoldLearnsWithoutSample(t *testing.T) {
	noSettle(t)
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
	u.measure = func() (procMem, bool) { return procMem{Resident: 28 * giB, Commit: 52 * giB}, true }
	reads := 0
	u.mem = func() (MemFact, bool) { // 20 GiB available before the stop, 52 GiB after: 32 GiB released
		reads++
		if reads == 1 {
			return MemFact{RAMAvailableGiB: 20, RAMTotalGiB: 64}, true
		}
		return MemFact{RAMAvailableGiB: 52, RAMTotalGiB: 64}, true
	}
	w.poll(context.Background(), time.Now()) // the server answers; no sample has run
	u.apply(true, time.Now())                // the router holds it at once
	m, err := loadUpstreamMemo(filepath.Join(dir, upstreamMemoFile))
	if err != nil || !m.known() {
		t.Fatalf("the hold remembered the models: %+v %v", m, err)
	}
	if m.ReleasedBytes != 32*giB || m.PeakResidentBytes != 28*giB || m.NeedBytes != withMargin(32*giB) {
		t.Fatalf("need = the larger of released (32) and resident (28) + margin: %+v", m)
	}
	if reads != 3 {
		t.Fatalf("RAM read before the stop, right after it and after the settle time: %d reads", reads)
	}
	sup2 := &Supervisor{children: map[string]*childState{"strata": {}, "ollama": {}}}
	sup2.Park("ollama")
	u2 := newUpstreamHold(sup2, "strata", fbCfg, newUpstreamSwitch(fbCfg), newOllamaWatcher(upSrv.URL, upstreamOpenAI, quietLogger()), quietLogger())
	u2.remember(dir)
	if !u2.startHeld(time.Now()) || !sup2.isHeld("strata") {
		t.Fatal("next start: held")
	}
}

// ADR-0092: a need an older agent stored as commit, or one learned under another start configuration, is discarded and
// learned again; the models stay, so the agent still starts held.
func TestNeedRelearned(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, upstreamMemoFile)
	tags := []json.RawMessage{json.RawMessage(`{"name":"big-moe"}`)}
	budget16 := []ChildSpec{{Name: "strata", Cmd: "strata.exe", Args: []string{"--resident-budget-gib", "16"}}, {Name: "ollama"}}
	setup := func(specs []ChildSpec) (*Supervisor, *upstreamHold) {
		sup := &Supervisor{specs: specs, children: map[string]*childState{"strata": {}, "ollama": {}}}
		sup.Park("ollama")
		pct := 90
		fbCfg := &FallbackCfg{Child: "ollama", Upstream: "http://127.0.0.1:11434", MaxRAMPercent: &pct}
		u := newUpstreamHold(sup, "strata", fbCfg, newUpstreamSwitch(fbCfg),
			newOllamaWatcher("http://127.0.0.1:9", upstreamOpenAI, quietLogger()), quietLogger())
		u.mem = func() (MemFact, bool) { return MemFact{RAMAvailableGiB: 10, RAMTotalGiB: 64}, true }
		return sup, u
	}

	// An older agent's file: the commit as need, no measure
	if err := saveUpstreamMemo(path, upstreamMemo{Tags: tags, NeedBytes: 52 * giB}); err != nil {
		t.Fatal(err)
	}
	sup, u := setup(budget16)
	u.remember(dir)
	if u.memo.NeedBytes != 0 || !u.memo.known() || u.ramBlock() != nil {
		t.Fatalf("commit of an older agent discarded, models kept, no check until learned: %+v", u.memo)
	}
	if !u.startHeld(time.Now()) || !sup.isHeld("strata") {
		t.Fatal("models remembered: still starts held")
	}
	m, _ := loadUpstreamMemo(path)
	if m.NeedBytes != 0 || m.Measure != needMeasure || m.Config != u.config || !m.known() {
		t.Fatalf("file rewritten with the new measure, models kept: %+v", m)
	}

	// Learned under budget 16; the same configuration keeps it
	m.PeakResidentBytes, m.NeedBytes = 28*giB, withMargin(28*giB)
	if err := saveUpstreamMemo(path, m); err != nil {
		t.Fatal(err)
	}
	_, u = setup(budget16)
	u.remember(dir)
	if u.memo.NeedBytes != withMargin(28*giB) || u.ramBlock() == nil {
		t.Fatalf("same configuration: need kept and checked: %+v", u.memo)
	}

	// Another budget: learned again
	budget8 := []ChildSpec{{Name: "strata", Cmd: "strata.exe", Args: []string{"--resident-budget-gib", "8"}}, {Name: "ollama"}}
	_, u = setup(budget8)
	u.remember(dir)
	if u.memo.NeedBytes != 0 || u.memo.PeakResidentBytes != 0 || !u.memo.known() || u.memo.Config != u.config {
		t.Fatalf("changed configuration: need learned again, models kept: %+v", u.memo)
	}
}

// The configuration print changes with every part of how the child starts, and only with that.
func TestChildConfig(t *testing.T) {
	print := func(sp ChildSpec) string {
		sp.Name = "strata"
		return (&Supervisor{specs: []ChildSpec{{Name: "ollama", Cmd: "ollama.exe"}, sp}}).childConfig("strata")
	}
	base := ChildSpec{Cmd: "strata.exe", Args: []string{"serve", "--resident-budget-gib", "16"}, Env: []string{"A=1"}}
	same := print(base)
	if same == "" || same != print(base) {
		t.Fatalf("stable print: %q", same)
	}
	changed := []ChildSpec{
		{Cmd: "strata2.exe", Args: base.Args, Env: base.Env},
		{Cmd: base.Cmd, Args: []string{"serve", "--resident-budget-gib", "8"}, Env: base.Env},
		{Cmd: base.Cmd, Args: base.Args, Env: []string{"A=2"}},
		{Cmd: base.Cmd, Cwd: "work", Args: base.Args, Env: base.Env},
		{Cmd: base.Cmd, Args: []string{"serve", "--resident-budget-gib"}, Env: []string{"16", "A=1"}}, // moved between lists
	}
	for _, sp := range changed {
		if print(sp) == same {
			t.Fatalf("change not seen: %+v", sp)
		}
	}
	if (&Supervisor{}).childConfig("strata") != "" {
		t.Fatal("no such child: empty print")
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
