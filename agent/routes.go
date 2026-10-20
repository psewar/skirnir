package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Tunnel-Routen (Vertrag Reranker, 2026-10-20): Ohne tunnel.routes geht jede Tunnel-Anfrage wie bisher an tunnel.upstream
// (Ollama). Mit routes gehen die gelisteten Pfade an ihren eigenen lokalen Dienst (z. B. den Reranker als Agent-Kind), und nur
// mit den gelisteten Methoden; eine andere Methode beantwortet der Agent selbst mit 405, ohne Ollama zu fragen. Der Agent prueft
// jeden Dienst alle 5 s ueber dessen Gesundheitspfad und meldet das Ergebnis im Heartbeat als
// routes: {<name>: {up, reason}}, damit der Router einen kranken Dienst gar nicht erst anfragt.

// Gruende in RouteHealth.Reason (leer, wenn up).
const (
	routeUnreachable    = "unreachable"     // keine Verbindung zum Upstream
	routeUnhealthy      = "unhealthy"       // Antwort, aber nicht 200 und kein bekannter Zustand
	routeStarting       = "starting"        // Dienst meldet "starting", oder der Agent hat noch nicht geprueft
	routeSelftestFailed = "selftest_failed" // Dienst meldet, dass seine Selbstpruefung gescheitert ist
)

const routeHealthEvery = 5 * time.Second

// RouteHealth ist der Eintrag je Route im Heartbeat-Feld routes.
type RouteHealth struct {
	Up     bool   `json:"up"`
	Reason string `json:"reason"`
}

type Routes struct {
	list   []RouteCfg
	byPath map[string]RouteCfg
	log    *Logger
	client *http.Client
	every  time.Duration

	mu     sync.Mutex
	health map[string]RouteHealth
}

// newRoutes: nil ohne Routen; alle Methoden sind nil-fest, damit Tunnel und Heartbeat dann genau wie bisher arbeiten.
func newRoutes(cfg []RouteCfg, log *Logger) *Routes {
	if len(cfg) == 0 {
		return nil
	}
	r := &Routes{list: cfg, byPath: map[string]RouteCfg{}, log: log, every: routeHealthEvery,
		client: &http.Client{Timeout: 2 * time.Second}, health: map[string]RouteHealth{}}
	for _, rc := range cfg {
		r.byPath[rc.Path] = rc
		r.health[rc.Name] = RouteHealth{Up: false, Reason: routeStarting} // bis zur ersten Pruefung nicht bereit
	}
	return r
}

// Lookup: Route fuer den Pfad einer Tunnel-Anfrage. Verglichen wird der Pfad ohne Query, exakt.
func (r *Routes) Lookup(path string) (RouteCfg, bool) {
	if r == nil {
		return RouteCfg{}, false
	}
	p, _, _ := strings.Cut(path, "?")
	rc, ok := r.byPath[p]
	return rc, ok
}

func (rc RouteCfg) allows(method string) bool {
	for _, m := range rc.Methods {
		if m == method {
			return true
		}
	}
	return false
}

// Run prueft alle Routen im Takt, bis ctx endet.
func (r *Routes) Run(ctx context.Context) {
	for {
		r.checkAll(ctx)
		select {
		case <-ctx.Done():
			return
		case <-time.After(r.every):
		}
	}
}

func (r *Routes) checkAll(ctx context.Context) {
	for _, rc := range r.list {
		h := r.check(ctx, rc)
		if ctx.Err() != nil {
			return // Abbruch beim Beenden ist kein Befund ueber den Dienst
		}
		r.mu.Lock()
		old := r.health[rc.Name]
		r.health[rc.Name] = h
		r.mu.Unlock()
		if old == h {
			continue
		}
		if h.Up {
			r.log.Infof("route %s: bereit (%s%s)", rc.Name, rc.Upstream, rc.Health)
		} else {
			r.log.Warnf("route %s: nicht bereit, Grund %s (%s%s)", rc.Name, h.Reason, rc.Upstream, rc.Health)
		}
	}
}

// check fragt den Gesundheitspfad ab. 200 heisst bereit; sonst gilt der Zustand, den der Dienst selbst im Feld status meldet
// (starting, selftest_failed), und ohne einen solchen "unhealthy".
func (r *Routes) check(ctx context.Context, rc RouteCfg) RouteHealth {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rc.Upstream+rc.Health, nil)
	if err != nil {
		return RouteHealth{Reason: routeUnhealthy} // URL ist beim Laden geprueft; bleibt fuer den Fall eines Programmfehlers
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return RouteHealth{Reason: routeUnreachable}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if resp.StatusCode == http.StatusOK {
		return RouteHealth{Up: true}
	}
	var body struct {
		Status string `json:"status"`
	}
	if json.Unmarshal(raw, &body) == nil && (body.Status == routeStarting || body.Status == routeSelftestFailed) {
		return RouteHealth{Reason: body.Status}
	}
	return RouteHealth{Reason: routeUnhealthy}
}

// Report: Zustand aller Routen fuer Heartbeat und /health; nil ohne Routen (dann fehlt das Feld ganz).
func (r *Routes) Report() map[string]RouteHealth {
	if r == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string]RouteHealth, len(r.health))
	for k, v := range r.health {
		out[k] = v
	}
	return out
}
