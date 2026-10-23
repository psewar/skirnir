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

// holdWait: how long Hold waits for the server's process tree to end. Stopping is a job kill; a few seconds suffice.
const holdWait = 30 * time.Second

type upstreamHold struct {
	sup   *Supervisor
	child string
	watch *OllamaWatcher
	log   *Logger

	mu   sync.Mutex // one order at a time, in arrival order
	held bool
}

func newUpstreamHold(sup *Supervisor, child string, watch *OllamaWatcher, log *Logger) *upstreamHold {
	if sup == nil || child == "" {
		return nil
	}
	return &upstreamHold{sup: sup, child: child, watch: watch, log: log}
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
		if stopped {
			u.log.Infof("upstream-hold: %s stopped to free the GPU (router order)", u.child)
		} else {
			u.log.Warnf("upstream-hold: %s did not stop within %s; held anyway, it will not be restarted", u.child, holdWait)
		}
		return
	}
	u.sup.Release(u.child)
	u.watch.SetHeld(false, now)
	u.held = false
	u.log.Infof("upstream-release: %s started again (router order)", u.child)
}
