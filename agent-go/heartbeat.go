package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

// Heartbeat: GPU-Fakten des Knotens und das Urteil des Routers (free/busy). Normalweg ist der Tunnel
// (tunnel.go schickt payload() als HB-Rahmen und ruft ack()); Run() ist der alte HTTP-Weg mit Token fuer
// Router ohne Tunnel-Anmeldung.
type Heartbeat struct {
	cfg   RouterCfg
	node  string
	gpu   *GPU
	log   *Logger
	http  *http.Client
	tlsFP string // Fingerprint der Ollama-Vorschaltstelle (leer = kein Proxy)
	guard *Guard // GPU-Schutz: Status geht mit jedem Heartbeat (0.7.0)

	mu         sync.Mutex
	state      string // Antwort des Routers: free | busy | pending | ""
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

func newHeartbeat(cfg RouterCfg, node string, gpu *GPU, log *Logger) *Heartbeat {
	return &Heartbeat{cfg: cfg, node: node, gpu: gpu, log: log,
		http: &http.Client{Timeout: time.Duration(cfg.TimeoutS * float64(time.Second))}}
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
	return p, nil
}

func (h *Heartbeat) markSent() {
	h.mu.Lock()
	h.sent++
	h.mu.Unlock()
}

// ack verarbeitet die Antwort des Routers (aus dem Tunnel oder per HTTP).
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

// Run: alter HTTP-Weg (POST /v1/heartbeat/<node> mit X-Router-Token), nur wenn der Tunnel abgeschaltet ist.
func (h *Heartbeat) Run(ctx context.Context) {
	h.log.Infof("heartbeat (HTTP, Token): node=%s router=%s interval=%.0fs", h.node, h.cfg.URL, h.cfg.IntervalS)
	t := time.NewTicker(time.Duration(h.cfg.IntervalS * float64(time.Second)))
	defer t.Stop()
	for {
		h.once(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (h *Heartbeat) once(ctx context.Context) {
	payload, err := h.payload(ctx)
	if err != nil {
		h.fail(err.Error())
		return
	}
	body, _ := json.Marshal(payload)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, h.cfg.URL+"/v1/heartbeat/"+h.node, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Router-Token", h.cfg.Token)
	resp, err := h.http.Do(req)
	if err != nil {
		h.fail(err.Error())
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	var r hbResponse
	_ = json.Unmarshal(raw, &r)
	if resp.StatusCode != 200 {
		h.fail(fmt.Sprintf("HTTP %d %s", resp.StatusCode, firstNonEmpty(r.Error, string(raw))))
		return
	}
	h.markSent()
	h.ack(r)
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
		st.OK = time.Since(h.lastOK) < 30*time.Second
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
