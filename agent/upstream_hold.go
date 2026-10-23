package main

import (
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

// holdWait: how long Hold waits for the server's process tree to end. Stopping is a job kill; a few seconds suffice.
const holdWait = 30 * time.Second

// fallbackDrain: how long a release waits for answers still running on the standby Ollama (a voice reply takes seconds,
// a long chat answer can take a minute) before it stops it anyway.
const fallbackDrain = 2 * time.Minute

type upstreamHold struct {
	sup   *Supervisor
	child string
	watch *OllamaWatcher
	log   *Logger

	fbChild string          // tunnel.upstream_fallback.child; "" = no standby
	fbURL   string          // its loopback URL
	sw      *upstreamSwitch // where the tunnel sends requests; nil without a standby

	mu        sync.Mutex // one order at a time, in arrival order
	held      bool
	fbRunning bool // the standby was started for this hold
}

func newUpstreamHold(sup *Supervisor, child string, fb *FallbackCfg, sw *upstreamSwitch, watch *OllamaWatcher, log *Logger) *upstreamHold {
	if sup == nil || child == "" {
		return nil
	}
	u := &upstreamHold{sup: sup, child: child, watch: watch, log: log}
	if fb != nil && sw != nil {
		u.fbChild, u.fbURL, u.sw = fb.Child, fb.Upstream, sw
	}
	return u
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
