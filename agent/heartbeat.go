package main

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// Heartbeat: GPU-Fakten des Knotens und das Urteil des Routers (free/busy). tunnel.go schickt payload() als HB-Rahmen
// und ruft ack(). Der alte HTTP-Weg mit Token (POST /v1/heartbeat/<node>) ist seit 0.9.1 weg.
type Heartbeat struct {
	node     string
	gpu      *GPU
	log      *Logger
	tlsFP    string         // Fingerprint der Ollama-Vorschaltstelle (leer = kein Proxy)
	guard    *Guard         // GPU-Schutz: Status geht mit jedem Heartbeat (0.7.0)
	updater  *Updater       // Selbst-Update: Zwischenstand geht mit dem Heartbeat (0.8.0)
	ollamaUp *OllamaUpdater // Ollama-Update: Zwischenstand (0.12.0)
	upstream string         // lokales Ollama: Version geht mit dem Heartbeat (0.12.0, alle 60 s frisch)

	mu         sync.Mutex
	ovVersion  string
	ovAt       time.Time
	every      time.Duration // Sendeintervall (Tunnel/HTTP); Status.OK = juengste Bestaetigung innerhalb 3 Intervallen
	state      string        // Antwort des Routers: free | busy | pending | ""
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
	GuardAck   *bool  `json:"gpu_guard_ack,omitempty"` // Router-Policy fuer den GPU-Schutz dieses Knotens (nil = Router kennt es nicht)
}

func newHeartbeat(node string, gpu *GPU, log *Logger) *Heartbeat {
	return &Heartbeat{node: node, gpu: gpu, log: log}
}

// SetInterval merkt das Sendeintervall (Provisionierung durch den Router), damit das OK-Fenster in Status() dazu passt.
func (h *Heartbeat) SetInterval(d time.Duration) {
	if d > 0 {
		h.mu.Lock()
		h.every = d
		h.mu.Unlock()
	}
}

// payload sammelt die GPU-Fakten fuer einen Heartbeat.
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
	if s.Sensors != nil { // 0.6.0: Temperatur, Leistung, Drosselung, GPU-Z - der Router legt den Block in den Knotenzustand
		p["sensors"] = s.Sensors
	}
	if h.guard != nil { // 0.7.0: Zustand des GPU-Schutzes
		p["gpu_guard"] = h.guard.Status()
	}
	if r := h.updater.Report(); r != nil { // 0.8.0: Stand eines Update-Auftrags
		p["update"] = r
	}
	if r := h.ollamaUp.Report(); r != nil { // 0.12.0: Stand eines Ollama-Update-Auftrags
		p["ollama_update"] = r
	}
	if v := h.ollamaVersionCached(ctx); v != "" { // 0.12.0: der Router sieht die Ollama-Version sofort, nicht erst beim Poll
		p["ollama_version"] = v
	}
	return p, nil
}

// ollamaVersionCached: /api/version des lokalen Ollama, hoechstens alle 60 s (auch ein Fehlschlag wird 60 s gemerkt).
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

// ack verarbeitet die Antwort des Routers auf einen Heartbeat.
func (h *Heartbeat) ack(r hbResponse) {
	h.mu.Lock()
	changed := r.State != h.state
	h.state, h.busyReason, h.lastOK, h.lastErr, h.failures = r.State, r.BusyReason, time.Now(), "", 0
	h.guardAck = r.GuardAck
	h.mu.Unlock()
	if changed {
		h.log.Infof("heartbeat: router sieht uns als '%s' %s", r.State, r.BusyReason)
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
		h.log.Warnf("heartbeat fehlgeschlagen (%d): %s", n, msg)
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
		window := 30 * time.Second // fest 30 s liess /healthz bei Intervallen ab 30 s dauerhaft 503 sagen (Review 2026-09-25)
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
