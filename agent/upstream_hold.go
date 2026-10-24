package main

import (
	"context"
	"encoding/json"
	"path/filepath"
	"sync"
	"time"
)

// Freeing the GPU from an OpenAI-compatible upstream (agent 0.22.0, router 0.11.0).
//
// Ollama unloads a model when the router asks (/api/generate keep_alive 0) and keeps running. An OpenAI-compatible
// server such as Strata has no such call: its model stays in GPU memory for as long as the process runs. So when the
// router marks the node busy (a game needs the card), it sends {"t":"upstream-hold"} and the agent stops the child
// that runs the server (tunnel.upstream_child) and keeps it stopped. When the node is free again, or a request needs
// the model, the router sends {"t":"upstream-release"} and the supervisor starts the child again.
//
// The watcher reports both as a fact (OLLAMA frame upstream_state "held" / "starting", up with nothing loaded), so
// the router sees "the node is there, its model is not" instead of inferring "down" from failed polls.
//
// Standby Ollama (agent 0.23.0, tunnel.upstream_fallback): a held upstream leaves the node with nothing to serve
// during a game, not even the small busy_ok model of the voice assistant. With a fallback child the agent starts that
// Ollama once the upstream has stopped and sends the tunnel's requests there; the watcher reports Ollama's models
// (serving "ollama"), so the router treats the node as an Ollama node until it releases the upstream. On release the
// agent lets running answers on the standby finish, stops it, and only then starts the upstream: the two never share
// the card. The standby is parked (held) from the agent's start on.
//
// Start held and RAM check (agent 0.26.0): with a standby and remembered upstream models (upstream_memo.go) the agent
// starts with the upstream held and the standby serving; the router releases the upstream when it wants it (a request
// for its model, its window). Before every release the agent checks that the RAM in use plus the upstream's measured
// need stays within tunnel.upstream_fallback.max_ram_percent; otherwise the upstream stays held and the watcher reports
// upstream_blocked until there is room again.

// holdWait: how long Hold waits for the server's process tree to end. Stopping is a job kill; a few seconds suffice.
const holdWait = 30 * time.Second

// fallbackDrain: how long a release waits for answers still running on the standby Ollama (a voice reply takes seconds,
// a long chat answer can take a minute) before it stops it anyway.
const fallbackDrain = 2 * time.Minute

// upstreamSampleEvery: how often the agent samples the running upstream (models, RAM need) and re-checks a block.
const upstreamSampleEvery = 30 * time.Second

type upstreamHold struct {
	sup   *Supervisor
	child string
	watch *OllamaWatcher
	log   *Logger

	fbChild string          // tunnel.upstream_fallback.child; "" = no standby
	fbURL   string          // its loopback URL
	sw      *upstreamSwitch // where the tunnel sends requests; nil without a standby

	// 0.26.0, only with a standby: what the agent remembers about the upstream and the RAM check before a release
	memoPath  string                 // <identity dir>/upstream.json; "" = nothing remembered, no held start
	memo      upstreamMemo           // models and RAM need (peak private bytes of the last run)
	runPeak   uint64                 // peak private bytes of the upstream in the current run
	maxRAMPct int                    // tunnel.upstream_fallback.max_ram_percent; 0 = no check
	mem       func() (MemFact, bool) // RAM now (memoryFact; tests replace it)
	private   func() (uint64, bool)  // private bytes of the running upstream (Supervisor.childPrivateBytes)
	blocked   *UpstreamBlocked       // the last release was refused for this reason; nil = not blocked

	mu        sync.Mutex // one order at a time, in arrival order
	held      bool
	fbRunning bool // the standby was started for this hold
}

func newUpstreamHold(sup *Supervisor, child string, fb *FallbackCfg, sw *upstreamSwitch, watch *OllamaWatcher, log *Logger) *upstreamHold {
	if sup == nil || child == "" {
		return nil
	}
	u := &upstreamHold{sup: sup, child: child, watch: watch, log: log, mem: memoryFact}
	u.private = func() (uint64, bool) { return sup.childPrivateBytes(child) }
	if fb != nil && sw != nil {
		u.fbChild, u.fbURL, u.sw = fb.Child, fb.Upstream, sw
		if fb.MaxRAMPercent != nil {
			u.maxRAMPct = *fb.MaxRAMPercent
		}
	}
	return u
}

// remember loads upstream.json from dir. Only with a standby: a held start without one would leave the node with
// nothing to serve. A broken file is reported and treated as none (the upstream starts once and the agent learns again).
func (u *upstreamHold) remember(dir string) {
	if u == nil || u.fbChild == "" || dir == "" {
		return
	}
	u.memoPath = filepath.Join(dir, upstreamMemoFile)
	m, err := loadUpstreamMemo(u.memoPath)
	if err != nil {
		u.log.Warnf("upstream: %s not readable (%v) - %s starts once and the agent learns its models and RAM need again", u.memoPath, err, u.child)
	}
	u.memo = m
}

// startHeld: with remembered models the agent starts with the upstream held and the standby serving, so a restart does
// not load the upstream (~38 GB for Strata) only for the router to hold it again. Call before the supervisor runs.
// false = nothing remembered: the upstream starts as before, and the agent learns its models and RAM need.
func (u *upstreamHold) startHeld(now time.Time) bool {
	if u == nil || u.memoPath == "" {
		return false
	}
	if !u.memo.known() {
		u.log.Infof("upstream: no remembered models of %s yet - it starts once so the agent learns them and its RAM need", u.child)
		return false
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	u.sup.Park(u.child)
	u.held = true
	u.watch.StartHeld(u.memo.Tags, now)
	u.startFallback(now)
	u.log.Infof("upstream: agent starts with %s held (%d remembered models, RAM need %.1f GiB); the router releases it when it wants it",
		u.child, len(u.memo.Tags), gib(u.memo.NeedBytes))
	return true
}

// ramBlock: would starting the upstream take the RAM over max_ram_percent? In use now plus the remembered need. The
// standby's own RAM counts although it stops first (its models sit in VRAM, the process holds little) - erring on the
// safe side. nil = no check configured, need not learned yet, RAM not measurable, or it fits.
func (u *upstreamHold) ramBlock() *UpstreamBlocked {
	if u.maxRAMPct <= 0 || u.memo.NeedBytes == 0 || u.mem == nil {
		return nil
	}
	m, ok := u.mem()
	if !ok || m.RAMTotalGiB <= 0 {
		return nil
	}
	need := gib(u.memo.NeedBytes)
	if m.RAMTotalGiB-m.RAMAvailableGiB+need <= m.RAMTotalGiB*float64(u.maxRAMPct)/100 {
		return nil
	}
	return &UpstreamBlocked{Reason: blockedMemory, RAMAvailableGiB: m.RAMAvailableGiB, RAMTotalGiB: m.RAMTotalGiB,
		NeedGiB: need, LimitPercent: u.maxRAMPct}
}

func (u *upstreamHold) setBlocked(b *UpstreamBlocked) {
	u.blocked = b
	u.watch.SetBlocked(b)
}

// Run samples the upstream every upstreamSampleEvery: while it runs, its models and RAM need for upstream.json; while it
// is held and blocked, whether the RAM has room again (then the block is cleared and the router may ask once more).
func (u *upstreamHold) Run(ctx context.Context) {
	if u == nil || u.memoPath == "" {
		return
	}
	t := time.NewTicker(upstreamSampleEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			u.sample()
		}
	}
}

func (u *upstreamHold) sample() {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.held {
		if u.blocked != nil && u.ramBlock() == nil {
			u.log.Infof("upstream: RAM has room for %s again - no longer blocked", u.child)
			u.setBlocked(nil)
		}
		return
	}
	changed := false
	if tags, ok := u.watch.UpstreamTags(); ok && !sameRaw(tags, u.memo.Tags) {
		u.memo.Tags, changed = tags, true
	}
	if p, ok := u.private(); ok && p > u.runPeak {
		u.runPeak = p
		if p > u.memo.NeedBytes {
			u.memo.NeedBytes, changed = p, true
		}
	}
	if changed {
		u.saveMemo()
	}
}

// endRun: the upstream is about to stop - its peak in this run becomes the remembered need (so a smaller setup lowers it).
func (u *upstreamHold) endRun() {
	if u.memoPath == "" {
		return
	}
	if p, ok := u.private(); ok && p > u.runPeak {
		u.runPeak = p
	}
	if u.runPeak > 0 && u.runPeak != u.memo.NeedBytes {
		u.memo.NeedBytes = u.runPeak
		u.saveMemo()
	}
	u.runPeak = 0
}

func (u *upstreamHold) saveMemo() {
	u.memo.Updated = time.Now().UTC()
	if err := saveUpstreamMemo(u.memoPath, u.memo); err != nil {
		u.log.Warnf("upstream: %s not written: %v", u.memoPath, err)
	}
}

func sameRaw(a, b []json.RawMessage) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if string(a[i]) != string(b[i]) {
			return false
		}
	}
	return true
}

// Handle executes a router order. Runs in its own goroutine (stopping waits for the process tree).
func (u *upstreamHold) Handle(hold bool) {
	if u == nil {
		return
	}
	go u.apply(hold, time.Now())
}

func (u *upstreamHold) apply(hold bool, now time.Time) {
	u.mu.Lock()
	defer u.mu.Unlock()
	if hold == u.held {
		return // repeated order (the router resends while its view lags): nothing to do
	}
	if hold {
		u.endRun()
		u.watch.SetHeld(true, now) // report first: no request should go to a server that is about to stop
		stopped := u.sup.Hold(u.child, holdWait)
		u.held = true
		if !stopped {
			u.log.Warnf("upstream-hold: %s did not stop within %s; held anyway, it will not be restarted", u.child, holdWait)
			if u.fbChild != "" {
				u.log.Warnf("upstream-hold: standby %s not started - the card is not free", u.fbChild)
			}
			return
		}
		u.log.Infof("upstream-hold: %s stopped to free the GPU (router order)", u.child)
		u.startFallback(now)
		return
	}
	if b := u.ramBlock(); b != nil {
		// Checked before anything changes: the standby keeps serving, the router gets the reason as a fact
		u.log.Warnf("upstream-release refused: %s needs %.1f GiB, %.1f of %.1f GiB available - RAM would exceed %d %%; stays held",
			u.child, b.NeedGiB, b.RAMAvailableGiB, b.RAMTotalGiB, b.LimitPercent)
		u.setBlocked(b)
		return
	}
	if u.blocked != nil {
		u.setBlocked(nil)
	}
	u.watch.SetHeld(false, now) // report first: the router stops sending to the standby while its answers finish
	if u.stopFallback() {
		u.watch.SetHeld(false, time.Now()) // the start grace counts from the upstream's start, not from the drain
	}
	u.sup.Release(u.child)
	u.held = false
	u.log.Infof("upstream-release: %s started again (router order)", u.child)
}

// startFallback: the upstream has stopped; start the standby and send the tunnel's requests to it.
func (u *upstreamHold) startFallback(now time.Time) {
	if u.fbChild == "" {
		return
	}
	u.sup.Release(u.fbChild)
	u.sw.set(true)
	u.watch.SetFallback(u.fbURL, now)
	u.fbRunning = true
	u.log.Infof("upstream-hold: standby %s started in place of %s", u.fbChild, u.child)
}

// stopFallback: let the answers running on the standby finish (at most fallbackDrain), then stop it, so the upstream
// gets the whole card. Requests stay on the standby until then: the router sends new ones elsewhere once the watcher
// reports the release. False = there was no standby to stop.
func (u *upstreamHold) stopFallback() bool {
	if !u.fbRunning {
		return false
	}
	if !u.sw.drain(fallbackDrain) {
		u.log.Warnf("upstream-release: answers on standby %s still running after %s; stopping it anyway", u.fbChild, fallbackDrain)
	}
	u.sw.set(false)
	if !u.sup.Hold(u.fbChild, holdWait) {
		u.log.Warnf("upstream-release: standby %s did not stop within %s", u.fbChild, holdWait)
	}
	u.fbRunning = false
	u.log.Infof("upstream-release: standby %s stopped", u.fbChild)
	return true
}

// upstreamSwitch: where the tunnel sends a request no route claims - the upstream, or the standby Ollama while it
// stands in. Counts the requests on the standby so a release can let them finish.
type upstreamSwitch struct {
	fallback string

	mu       sync.Mutex
	on       bool // the standby serves
	inflight int  // requests on the standby
}

func newUpstreamSwitch(fb *FallbackCfg) *upstreamSwitch {
	if fb == nil {
		return nil
	}
	return &upstreamSwitch{fallback: fb.Upstream}
}

// acquire returns the target for one request and the function to call when it ends. A nil switch is the upstream.
func (s *upstreamSwitch) acquire(upstream string) (string, func()) {
	if s == nil {
		return upstream, func() {}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.on {
		return upstream, func() {}
	}
	s.inflight++
	var once sync.Once
	return s.fallback, func() {
		once.Do(func() {
			s.mu.Lock()
			s.inflight--
			s.mu.Unlock()
		})
	}
}

func (s *upstreamSwitch) set(on bool) {
	s.mu.Lock()
	s.on = on
	s.mu.Unlock()
}

// drain waits until no request runs on the standby; false = still some after wait.
func (s *upstreamSwitch) drain(wait time.Duration) bool {
	deadline := time.Now().Add(wait)
	for {
		s.mu.Lock()
		n := s.inflight
		s.mu.Unlock()
		if n == 0 {
			return true
		}
		if !time.Now().Before(deadline) {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
}
