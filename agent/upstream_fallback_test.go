package main

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// While the upstream is held, the standby Ollama's models are reported (serving "ollama", state "held") together with
// the held upstream's models; a silent standby leaves a plain hold, never "down"; the release brings back the upstream's
// models only.
func TestWatcherFallback(t *testing.T) {
	up := &fakeOpenAI{}
	up.set(`{"id":"big-moe","status":{"value":"loaded"}}`)
	upSrv := httptest.NewServer(up)
	defer upSrv.Close()
	fb := &fakeOllama{}
	fb.set(`{"name":"small:8b","size":5000000000,"size_vram":5000000000}`, `{"name":"small:8b"}`, false)
	fbSrv := httptest.NewServer(fb)
	defer fbSrv.Close()

	w := newOllamaWatcher(upSrv.URL, upstreamOpenAI, quietLogger())
	ctx := context.Background()
	now := time.Now()
	w.poll(ctx, now)
	w.SetHeld(true, now)
	w.SetFallback(fbSrv.URL, now)
	w.poll(ctx, now)
	st, _ := w.Snapshot()
	tags := string(joinRaw(st.Tags))
	if !st.Up || st.Serving != servingOllama || st.UpstreamState != upstreamHeld || len(st.PS) != 1 ||
		!strings.Contains(tags, "small:8b") || !strings.Contains(tags, "big-moe") {
		t.Fatalf("standby serving: its models loaded, the upstream's still known: %+v tags=%s", st, tags)
	}
	rev := st.Rev
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.Rev != rev {
		t.Fatalf("unchanged standby: no new revision (%d -> %d)", rev, st.Rev)
	}

	fb.set("", "", true) // the standby fails
	w.poll(ctx, now.Add(time.Second))
	if st, _ = w.Snapshot(); st.Serving != servingOllama {
		t.Fatalf("within its start grace a silent standby still serves: %+v", st)
	}
	for i := 0; i < 3; i++ {
		w.poll(ctx, now.Add(fallbackStartGrace+time.Second))
	}
	if st, _ = w.Snapshot(); !st.Up || st.Serving != "" || st.UpstreamState != upstreamHeld || len(st.PS) != 0 ||
		len(st.Tags) != 1 {
		t.Fatalf("silent standby: plain hold, never down: %+v", st)
	}
	fb.set(`{"name":"small:8b","size_vram":1}`, `{"name":"small:8b"}`, false)
	w.poll(ctx, now.Add(fallbackStartGrace+2*time.Second))
	if st, _ = w.Snapshot(); st.Serving != servingOllama {
		t.Fatalf("standby answering again: serving: %+v", st)
	}

	w.SetHeld(false, now)
	st, _ = w.Snapshot()
	if st.Serving != "" || st.UpstreamState != upstreamStarting || len(st.PS) != 0 || len(st.Tags) != 1 ||
		!strings.Contains(string(st.Tags[0]), "big-moe") {
		t.Fatalf("released: starting, only the upstream's models: %+v", st)
	}
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.UpstreamState != "" || st.Serving != "" || len(st.PS) != 1 ||
		!strings.Contains(string(st.PS[0]), "big-moe") {
		t.Fatalf("upstream answering again: %+v", st)
	}
}

func joinRaw(r []json.RawMessage) []byte {
	var b []byte
	for _, x := range r {
		b = append(append(b, x...), '\n')
	}
	return b
}

// Hold stops the upstream and starts the standby, the tunnel then sends requests to the standby; release waits for the
// standby's running requests, stops it, and starts the upstream again.
func TestUpstreamHoldWithFallback(t *testing.T) {
	sup := &Supervisor{children: map[string]*childState{"strata": {}, "ollama": {}}}
	sup.Park("ollama")
	fbCfg := &FallbackCfg{Child: "ollama", Upstream: "http://127.0.0.1:11434"}
	sw := newUpstreamSwitch(fbCfg)
	w := newOllamaWatcher("http://127.0.0.1:9", upstreamOpenAI, quietLogger())
	u := newUpstreamHold(sup, "strata", fbCfg, sw, w, quietLogger())
	if target, done := sw.acquire("http://127.0.0.1:8080"); target != "http://127.0.0.1:8080" {
		t.Fatalf("before a hold the upstream serves: %s", target)
	} else {
		done()
	}

	now := time.Now()
	u.apply(true, now)
	if !sup.isHeld("strata") || sup.isHeld("ollama") || !u.fbRunning {
		t.Fatalf("hold: upstream held, standby running: strata=%v ollama=%v", sup.isHeld("strata"), sup.isHeld("ollama"))
	}
	target, done := sw.acquire("http://127.0.0.1:8080")
	if target != "http://127.0.0.1:11434" {
		t.Fatalf("held: requests go to the standby, got %s", target)
	}

	released := make(chan struct{})
	go func() { u.apply(false, now); close(released) }()
	select {
	case <-released:
		t.Fatal("release did not wait for the request running on the standby")
	case <-time.After(500 * time.Millisecond):
	}
	if st, _ := w.Snapshot(); st.UpstreamState != upstreamStarting {
		t.Fatalf("release is reported before the drain, so the router stops sending: %+v", st)
	}
	done()
	done() // a second call must not count twice
	select {
	case <-released:
	case <-time.After(5 * time.Second):
		t.Fatal("release did not finish after the request ended")
	}
	if sup.isHeld("strata") || !sup.isHeld("ollama") || u.fbRunning {
		t.Fatalf("released: upstream running, standby held: strata=%v ollama=%v", sup.isHeld("strata"), sup.isHeld("ollama"))
	}
	if target, done := sw.acquire("http://127.0.0.1:8080"); target != "http://127.0.0.1:8080" {
		t.Fatalf("after the release the upstream serves: %s", target)
	} else {
		done()
	}
	if sw.inflight != 0 {
		t.Fatalf("inflight count %d after all requests ended", sw.inflight)
	}
}

// A parked child does not start until it is released.
func TestSupervisorParkedChild(t *testing.T) {
	sup := &Supervisor{children: map[string]*childState{"ollama": {}}}
	sup.Park("ollama")
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	defer cancel()
	if sup.waitWhileHeld(ctx, "ollama") {
		t.Fatal("parked child passed the wait without a release")
	}
	sup.Release("ollama")
	if !sup.waitWhileHeld(context.Background(), "ollama") {
		t.Fatal("released child still waits")
	}
}

func TestConfigUpstreamFallback(t *testing.T) {
	base := "router:\n  url: https://r.example.net\nchildren:\n  - name: strata\n    cmd: strata.exe\n" +
		"  - name: ollama\n    cmd: C:/Users/x/AppData/Local/Programs/Ollama/ollama.exe\n    args: [serve]\n" +
		"  - name: stt\n    cmd: python.exe\n"
	tun := "tunnel:\n  upstream: http://127.0.0.1:8080\n  upstream_api: openai\n  upstream_child: strata\n"
	c, err := loadConfig(writeCfg(t, base+tun+"  upstream_fallback:\n    child: ollama\n"))
	if err != nil {
		t.Fatalf("valid upstream_fallback refused: %v", err)
	}
	if c.Tunnel.UpstreamFallback.Upstream != "http://127.0.0.1:11434" {
		t.Fatalf("default standby URL: %q", c.Tunnel.UpstreamFallback.Upstream)
	}
	bad := map[string]string{
		"no upstream child":  base + "tunnel:\n  upstream_api: openai\n  upstream_fallback:\n    child: ollama\n",
		"the upstream child": base + tun + "  upstream_fallback:\n    child: strata\n",
		"not a child":        base + tun + "  upstream_fallback:\n    child: other\n",
		"not ollama":         base + tun + "  upstream_fallback:\n    child: stt\n",
		"not loopback":       base + tun + "  upstream_fallback:\n    child: ollama\n    upstream: http://192.168.1.5:11434\n",
		"disabled": strings.Replace(base, "args: [serve]", "args: [serve]\n    enabled: false", 1) + tun +
			"  upstream_fallback:\n    child: ollama\n",
	}
	for name, body := range bad {
		if _, err := loadConfig(writeCfg(t, body)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}
