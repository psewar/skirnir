package main

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"
)

// A held upstream is reported at once as up with nothing loaded ("held") and is not polled; after release it is
// "starting" until it answers, and only after the grace does silence count as down.
func TestWatcherHeldAndStarting(t *testing.T) {
	f := &fakeOpenAI{}
	f.set(`{"id":"big-moe","status":{"value":"loaded"},"meta":{"n_ctx":8192}}`)
	srv := httptest.NewServer(f)
	w := newOllamaWatcher(srv.URL, upstreamOpenAI, quietLogger())
	ctx := context.Background()
	now := time.Now()
	w.poll(ctx, now)
	if st, _ := w.Snapshot(); !st.Up || len(st.PS) != 1 || st.UpstreamState != "" {
		t.Fatalf("answering server: loaded, no upstream state: %+v", st)
	}

	w.SetHeld(true, now)
	st, _ := w.Snapshot()
	if !st.Up || len(st.PS) != 0 || st.UpstreamState != upstreamHeld || len(st.Tags) != 1 {
		t.Fatalf("held: up, nothing loaded, models still known: %+v", st)
	}
	rev := st.Rev
	srv.Close() // the server is gone while held
	for i := 0; i < 5; i++ {
		w.poll(ctx, now.Add(time.Duration(i)*time.Minute))
	}
	if st, _ = w.Snapshot(); !st.Up || st.UpstreamState != upstreamHeld || st.Rev != rev {
		t.Fatalf("held: not polled, never down, no new revision: %+v", st)
	}

	w.SetHeld(false, now)
	if st, _ = w.Snapshot(); !st.Up || st.UpstreamState != upstreamStarting {
		t.Fatalf("released: starting: %+v", st)
	}
	for i := 0; i < 5; i++ {
		w.poll(ctx, now.Add(time.Minute)) // within the grace: no answer is not down
	}
	if st, _ = w.Snapshot(); !st.Up || st.UpstreamState != upstreamStarting {
		t.Fatalf("starting within grace stays up: %+v", st)
	}
	for i := 0; i < 3; i++ {
		w.poll(ctx, now.Add(upstreamStartGrace+time.Second))
	}
	if st, _ = w.Snapshot(); st.Up {
		t.Fatalf("after the grace a silent server is down: %+v", st)
	}

	srv2 := httptest.NewServer(f) // answers again (new address: point the watcher at it)
	defer srv2.Close()
	w.upstream = srv2.URL
	w.poll(ctx, now.Add(upstreamStartGrace+2*time.Second))
	if st, _ = w.Snapshot(); !st.Up || st.UpstreamState != "" || len(st.PS) != 1 {
		t.Fatalf("answering again: normal state, model loaded: %+v", st)
	}
}

// Router orders on an agent whose supervisor does not run the child (here: none at all): the state is still reported,
// and a repeated order changes nothing.
func TestUpstreamHoldOrders(t *testing.T) {
	sup := &Supervisor{children: map[string]*childState{}} // no child running: Hold reports "not stopped"
	w := newOllamaWatcher("http://127.0.0.1:9", upstreamOpenAI, quietLogger())
	u := newUpstreamHold(sup, "strata", w, quietLogger())
	now := time.Now()
	u.apply(true, now)
	st, _ := w.Snapshot()
	if !u.held || st.UpstreamState != upstreamHeld {
		t.Fatalf("hold: %v %+v", u.held, st)
	}
	rev := st.Rev
	u.apply(true, now)
	if st, _ = w.Snapshot(); st.Rev != rev {
		t.Fatalf("repeated hold changes nothing: %d -> %d", rev, st.Rev)
	}
	u.apply(false, now)
	if st, _ = w.Snapshot(); u.held || st.UpstreamState != upstreamStarting {
		t.Fatalf("release: %v %+v", u.held, st)
	}
	if newUpstreamHold(sup, "", w, quietLogger()) != nil {
		t.Fatal("no child configured -> no hold handler (fact upstream_release stays false)")
	}
}

func TestConfigUpstreamChild(t *testing.T) {
	base := "router:\n  url: https://r.example.net\nchildren:\n  - name: strata\n    cmd: strata.exe\n"
	ok := base + "tunnel:\n  upstream: http://127.0.0.1:8080\n  upstream_api: openai\n  upstream_child: strata\n"
	if _, err := loadConfig(writeCfg(t, ok)); err != nil {
		t.Fatalf("valid upstream_child refused: %v", err)
	}
	if _, err := loadConfig(writeCfg(t, base+"tunnel:\n  upstream_api: openai\n  upstream_child: other\n")); err == nil {
		t.Fatal("upstream_child that is no child accepted")
	}
	if _, err := loadConfig(writeCfg(t, base+"tunnel:\n  upstream_child: strata\n")); err == nil {
		t.Fatal("upstream_child with an Ollama upstream accepted")
	}
}
