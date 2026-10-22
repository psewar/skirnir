package main

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// Heartbeat: node GPU facts and router verdict (free/busy). tunnel.go sends payload() as the HB frame
// and calls ack(). The old HTTP path with token (POST /v1/heartbeat/<node>) was removed in 0.9.1.
type Heartbeat struct {
	node     string
	gpu      *GPU
	log      *Logger
	tlsFP    string         // Fingerprint of the Ollama proxy (empty = no proxy)
	guard    *Guard         // GPU guard: status sent with every heartbeat (0.7.0)
	updater  *Updater       // Self-update: progress sent with the heartbeat (0.8.0)
	ollamaUp *OllamaUpdater // Ollama-Update: Zwischenstand (0.12.0)
	upstream string         // Local Ollama: version sent with the heartbeat (0.12.0, refreshed every 60 s)
	sup      *Supervisor    // 0.13.0: VRAM of own children (except Ollama) as a fact
	game     *GameWatch     // 0.17.0: active game as a fact (game.go)
	routes   *Routes        // tunnel.routes: health of routed services (routes.go); nil = field missing

	mu         sync.Mutex
	ovVersion  string
	ovAt       time.Time
	every      time.Duration // Send interval (Tunnel/HTTP); Status.OK = latest ack within 3 intervals
	state      string        // Router response: free | busy | pending | ""
	busyReason string
	lastOK     time.Time
	lastErr    string
	failures   int
	sent       int
	guardAck   *bool
}

type hbResponse struct {
	State      string `json:"state"`
	BusyReason string `json:"busy_reason"`
	Error      string `json:"error"`
	GuardAck   *bool  `json:"gpu_guard_ack,omitempty"` // Router policy for this node's GPU guard (nil = router doesn't know it)
}

func newHeartbeat(node string, gpu *GPU, log *Logger) *Heartbeat {
	return &Heartbeat{node: node, gpu: gpu, log: log}
}

// SetInterval stores the send interval (provisioned by the router) so the OK window in Status() matches.
func (h *Heartbeat) SetInterval(d time.Duration) {
	if d > 0 {
		h.mu.Lock()
		h.every = d
		h.mu.Unlock()
	}
}

// payload collects GPU facts for a heartbeat.
func (h *Heartbeat) payload(ctx context.Context) (map[string]any, error) {
	s, err := h.gpu.Sample(ctx)
	if err != nil {
		return nil, fmt.Errorf("nvidia-smi: %w", err)
	}
	p := map[string]any{
		"gpu_util_pct": s.UtilPct, "vram_total_mib": s.TotalMiB, "vram_used_mib": s.UsedMiB,
		"vram_free_mib": s.FreeMiB, "ts": time.Now().Unix(),
	}
	if h.tlsFP != "" {
		p["ollama_tls_sha256"] = h.tlsFP
	}
	if s.Sensors != nil { // 0.6.0: temperature, power, throttling, GPU-Z - the router stores the block in node state
		p["sensors"] = s.Sensors
	}
	if h.guard != nil { // 0.7.0: GPU guard status
		p["gpu_guard"] = h.guard.Status()
	}
	if r := h.updater.Report(); r != nil { // 0.8.0: Stand eines Update-Auftrags
		p["update"] = r
	}
	if r := h.ollamaUp.Report(); r != nil { // 0.12.0: Stand eines Ollama-Update-Auftrags
		p["ollama_update"] = r
	}
	if v := h.ollamaVersionCached(ctx); v != "" { // 0.12.0: router sees the Ollama version immediately, not just on poll
		p["ollama_version"] = v
	}
	if h.sup != nil { // 0.13.0: router does not count this memory as foreign (otherwise the STT service looks like a game)
		if m := h.sup.childrenVRAM(); len(m) > 0 {
			p["children_vram_mib"] = m
		}
	}
	if h.game != nil { // 0.17.0: game and RAM - router marks node busy or avoids cold loads
		p["game"] = h.game.Fact()
	}
	if m, ok := memoryFact(); ok {
		p["memory"] = m
	}
	if r := h.routes.Report(); r != nil { // Tunnel routes: router only selects nodes whose service reports up
		p["routes"] = r
	}
	return p, nil
}

// ollamaVersionCached: /api/version of local Ollama, max every 60 s (failures also cached for 60 s).
func (h *Heartbeat) ollamaVersionCached(ctx context.Context) string {
	if h.upstream == "" {
		return ""
	}
	h.mu.Lock()
	v, at := h.ovVersion, h.ovAt
	h.mu.Unlock()
	if time.Since(at) < 60*time.Second {
		return v
	}
	v = ollamaVersion(ctx, h.upstream)
	h.mu.Lock()
	h.ovVersion, h.ovAt = v, time.Now()
	h.mu.Unlock()
	return v
}

func (h *Heartbeat) markSent() {
	h.mu.Lock()
	h.sent++
	h.mu.Unlock()
}

// ack processes the router's response to a heartbeat.
func (h *Heartbeat) ack(r hbResponse) {
	h.mu.Lock()
	changed := r.State != h.state
	h.state, h.busyReason, h.lastOK, h.lastErr, h.failures = r.State, r.BusyReason, time.Now(), "", 0
	h.guardAck = r.GuardAck
	h.mu.Unlock()
	if changed {
		h.log.Infof("heartbeat: router sees us as '%s' %s", r.State, r.BusyReason)
	}
}

func (h *Heartbeat) fail(msg string) {
	h.mu.Lock()
	h.failures++
	n := h.failures
	h.lastErr = msg
	h.state = ""
	h.mu.Unlock()
	if n == 1 || n%100 == 0 {
		h.log.Warnf("heartbeat failed (%d): %s", n, msg)
	}
}

type HeartbeatStatus struct {
	State      string `json:"state"`
	BusyReason string `json:"busy_reason"`
	LastOK     string `json:"last_ok"`
	LastError  string `json:"last_error,omitempty"`
	Failures   int    `json:"failures"`
	Sent       int    `json:"sent"`
	OK         bool   `json:"ok"`
	GuardAck   *bool  `json:"gpu_guard_ack,omitempty"`
}

func (h *Heartbeat) Status() HeartbeatStatus {
	h.mu.Lock()
	defer h.mu.Unlock()
	st := HeartbeatStatus{State: h.state, BusyReason: h.busyReason, LastError: h.lastErr, Failures: h.failures, Sent: h.sent, GuardAck: h.guardAck}
	if !h.lastOK.IsZero() {
		st.LastOK = h.lastOK.Format(time.RFC3339)
		window := 30 * time.Second // Fixed 30 s caused /healthz to return 503 persistently for intervals >= 30 s (Review 2026-09-25)
		if 3*h.every > window {
			window = 3 * h.every
		}
		st.OK = time.Since(h.lastOK) < window
	}
	return st
}

func firstNonEmpty(a ...string) string {
	for _, s := range a {
		if s != "" {
			return s
		}
	}
	return ""
}
