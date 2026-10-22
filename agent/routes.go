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

// Tunnel routes (Reranker contract, 2026-10-20): Without tunnel.routes, all tunnel requests go to tunnel.upstream
// (Ollama) as before. With routes, listed paths go to their own local service (e.g., the Reranker as an agent child), and only
// with the listed methods; any other method is answered by the agent itself with 405, without asking Ollama. The agent checks
// each service every 5 s via its health path and reports the result in the Heartbeat as
// routes: {<name>: {up, reason}}, so the router does not query a sick service at all.

// Reasons in RouteHealth.Reason (empty if up).
const (
	routeUnreachable    = "unreachable"     // no connection to upstream
	routeUnhealthy      = "unhealthy"       // response, but not 200 and no known state
	routeStarting       = "starting"        // service reports "starting", or the agent has not checked yet
	routeSelftestFailed = "selftest_failed" // service reports that its self-check failed
)

const routeHealthEvery = 5 * time.Second

// RouteHealth is the entry per route in the Heartbeat field routes.
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

// newRoutes: nil without routes; all methods are nil-safe, so Tunnel and Heartbeat work exactly as before.
func newRoutes(cfg []RouteCfg, log *Logger) *Routes {
	if len(cfg) == 0 {
		return nil
	}
	r := &Routes{list: cfg, byPath: map[string]RouteCfg{}, log: log, every: routeHealthEvery,
		client: &http.Client{Timeout: 2 * time.Second}, health: map[string]RouteHealth{}}
	for _, rc := range cfg {
		r.byPath[rc.Path] = rc
		r.health[rc.Name] = RouteHealth{Up: false, Reason: routeStarting} // not ready until the first check
	}
	return r
}

// Lookup: Route for the path of a tunnel request. Compared is the path without query, exact.
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

// Run checks all routes in the tick, until ctx ends.
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
			return // Abort on shutdown is not a finding about the service
		}
		r.mu.Lock()
		old := r.health[rc.Name]
		r.health[rc.Name] = h
		r.mu.Unlock()
		if old == h {
			continue
		}
		if h.Up {
			r.log.Infof("route %s: ready (%s%s)", rc.Name, rc.Upstream, rc.Health)
		} else {
			r.log.Warnf("route %s: not ready, reason %s (%s%s)", rc.Name, h.Reason, rc.Upstream, rc.Health)
		}
	}
}

// check queries the health path. 200 means ready; otherwise the state reported by the service itself in the status field applies
// (starting, selftest_failed), and without such a state "unhealthy".
func (r *Routes) check(ctx context.Context, rc RouteCfg) RouteHealth {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rc.Upstream+rc.Health, nil)
	if err != nil {
		return RouteHealth{Reason: routeUnhealthy} // URL is checked on load; remains for the case of a programming error
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

// Report: State of all routes for Heartbeat and /health; nil without routes (then the field is missing entirely).
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
