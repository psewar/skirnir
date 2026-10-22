package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

const routeCfgOK = "router:\n  url: https://r.example.net\ntunnel:\n  routes:\n" +
	"    - name: rerank\n      path: /rerank\n      methods: [post]\n      upstream: http://127.0.0.1:8090/\n      health: /health\n"

func TestRoutesConfig(t *testing.T) {
	c, err := loadConfig(writeCfg(t, routeCfgOK))
	if err != nil {
		t.Fatalf("gueltige Route abgelehnt: %v", err)
	}
	r := c.Tunnel.Routes[0]
	if r.Upstream != "http://127.0.0.1:8090" || len(r.Methods) != 1 || r.Methods[0] != "POST" || c.Tunnel.Upstream != "http://127.0.0.1:11434" {
		t.Fatalf("Normalisierung: %+v, tunnel.upstream=%s", r, c.Tunnel.Upstream)
	}
	if _, err := loadConfig(writeCfg(t, strings.Replace(routeCfgOK, "127.0.0.1:8090/", "[::1]:8090", 1))); err != nil {
		t.Fatalf("::1 abgelehnt: %v", err)
	}
	// without routes: Config as before, no routes
	c, err = loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\n"))
	if err != nil || len(c.Tunnel.Routes) != 0 {
		t.Fatalf("ohne routes: %v %+v", err, c.Tunnel.Routes)
	}
	bad := map[string][2]string{
		"Upstream im Netz":        {"http://127.0.0.1:8090/", "http://192.0.2.10:8090"},
		"localhost":               {"http://127.0.0.1:8090/", "http://localhost:8090"},
		"anderes Loopback-Netz":   {"http://127.0.0.1:8090/", "http://127.0.0.2:8090"},
		"Upstream mit Pfad":       {"http://127.0.0.1:8090/", "http://127.0.0.1:8090/x"},
		"Upstream mit Zugang":     {"http://127.0.0.1:8090/", "http://nutzer@127.0.0.1:8090"},
		"kein http":               {"http://127.0.0.1:8090/", "ftp://127.0.0.1:8090"},
		"Pfad ohne Schraegstrich": {"path: /rerank", "path: rerank"},
		"Pfad mit Query":          {"path: /rerank", "path: /rerank?x=1"},
		"Methode unbekannt":       {"[post]", "[FETCH]"},
		"ohne Methoden":           {"[post]", "[]"},
		"Name fehlt":              {"name: rerank", "name: \"\""},
		"Name mit Grossbuchstabe": {"name: rerank", "name: Rerank"},
		"health fehlt":            {"health: /health", "health: \"\""},
	}
	for what, rep := range bad {
		body := strings.Replace(routeCfgOK, rep[0], rep[1], 1)
		if body == routeCfgOK {
			t.Fatalf("%s: Ersetzung griff nicht", what)
		}
		if _, err := loadConfig(writeCfg(t, body)); err == nil || !strings.Contains(err.Error(), "tunnel.routes") {
			t.Errorf("%s: akzeptiert oder falsche Meldung: %v", what, err)
		}
	}
	dup := routeCfgOK + "    - name: rerank\n      path: /other\n      methods: [POST]\n      upstream: http://127.0.0.1:8091\n      health: /health\n"
	if _, err := loadConfig(writeCfg(t, dup)); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Errorf("doppelter Name akzeptiert: %v", err)
	}
	dup = routeCfgOK + "    - name: other\n      path: /rerank\n      methods: [POST]\n      upstream: http://127.0.0.1:8091\n      health: /health\n"
	if _, err := loadConfig(writeCfg(t, dup)); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Errorf("doppelter Pfad akzeptiert: %v", err)
	}
}

// The commented-out example in config.example.yaml must load when uncommented and yield exactly the contract route.
func TestRoutesExampleConfig(t *testing.T) {
	raw, err := os.ReadFile("config.example.yaml")
	if err != nil {
		t.Fatal(err)
	}
	var lines []string
	inRoutes := false
	for _, l := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(l, "  # routes:") {
			inRoutes = true
		} else if inRoutes && !strings.HasPrefix(l, "  #  ") {
			inRoutes = false
		}
		if inRoutes {
			l = "  " + strings.TrimPrefix(l, "  # ")
		}
		lines = append(lines, l)
	}
	c, err := loadConfig(writeCfg(t, strings.Join(lines, "\n")))
	if err != nil {
		t.Fatalf("Beispiel laedt nicht: %v", err)
	}
	want := RouteCfg{Name: "rerank", Path: "/rerank", Methods: []string{"POST"}, Upstream: "http://127.0.0.1:8090", Health: "/health"}
	if len(c.Tunnel.Routes) != 1 || fmt.Sprint(c.Tunnel.Routes[0]) != fmt.Sprint(want) {
		t.Fatalf("Beispiel-Routen: %+v", c.Tunnel.Routes)
	}
}

// countingServer counts calls and responds with fixed text.
type countingServer struct {
	mu    sync.Mutex
	hits  []string
	reply string
}

func (s *countingServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.hits = append(s.hits, r.Method+" "+r.URL.RequestURI())
	s.mu.Unlock()
	w.Write([]byte(s.reply))
}

func (s *countingServer) calls() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string{}, s.hits...)
}

type tunFrame struct {
	typ     byte
	payload []byte
}

// runHandle sends a request through Tunnel.handle and collects frames until END or ERR.
func runHandle(t *testing.T, tun *Tunnel, method, path, body string) []tunFrame {
	t.Helper()
	head, _ := json.Marshal(map[string]any{"method": method, "path": path, "headers": map[string]string{"Content-Type": "application/json"}})
	payload := append(append(head, '\n'), body...)
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		tun.handle(context.Background(), conn, 7, payload)
	}))
	defer srv.Close()
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	var out []tunFrame
	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			t.Fatalf("Rahmen lesen nach %d Rahmen: %v", len(out), err)
		}
		if len(data) < 5 || binary.BigEndian.Uint32(data[1:5]) != 7 {
			t.Fatalf("Rahmen ungueltig: %q", data)
		}
		out = append(out, tunFrame{data[0], data[5:]})
		if data[0] == tunEnd || data[0] == tunErr {
			return out
		}
	}
}

// respOf: Status and headers from the RESP frame, body from the DATA frames.
func respOf(t *testing.T, fr []tunFrame) (int, map[string]string, string) {
	t.Helper()
	if len(fr) < 2 || fr[0].typ != tunResp || fr[len(fr)-1].typ != tunEnd {
		t.Fatalf("Rahmenfolge: %+v", fr)
	}
	var h struct {
		Status  int               `json:"status"`
		Headers map[string]string `json:"headers"`
	}
	if err := json.Unmarshal(fr[0].payload, &h); err != nil {
		t.Fatal(err)
	}
	var b strings.Builder
	for _, f := range fr[1 : len(fr)-1] {
		b.Write(f.payload)
	}
	return h.Status, h.Headers, b.String()
}

func newTestTunnel(upstream string, routes *Routes) *Tunnel {
	return &Tunnel{upstream: upstream, log: quietLogger(), http: &http.Client{}, streams: map[uint32]context.CancelFunc{}, routes: routes}
}

func TestTunnelRoutes(t *testing.T) {
	ollama := &countingServer{reply: `{"from":"ollama"}`}
	osrv := httptest.NewServer(ollama)
	defer osrv.Close()
	rerank := &countingServer{reply: `[{"index":0,"score":0.9}]`}
	rsrv := httptest.NewServer(rerank)
	defer rsrv.Close()
	routes := newRoutes([]RouteCfg{{Name: "rerank", Path: "/rerank", Methods: []string{"POST"}, Upstream: rsrv.URL, Health: "/health"}}, quietLogger())

	// without routes: everything goes to Ollama, including /rerank (behavior as before)
	st, _, body := respOf(t, runHandle(t, newTestTunnel(osrv.URL, nil), "POST", "/rerank", `{}`))
	if st != 200 || body != ollama.reply || len(rerank.calls()) != 0 {
		t.Fatalf("ohne routes: %d %q, Reranker-Aufrufe %v", st, body, rerank.calls())
	}

	tun := newTestTunnel(osrv.URL, routes)
	st, _, body = respOf(t, runHandle(t, tun, "POST", "/rerank", `{"query":"q","texts":["a"]}`))
	if st != 200 || body != rerank.reply {
		t.Fatalf("POST /rerank: %d %q", st, body)
	}
	st, _, body = respOf(t, runHandle(t, tun, "POST", "/rerank?x=1", `{}`))
	if st != 200 || body != rerank.reply {
		t.Fatalf("POST /rerank?x=1 ging nicht an den Reranker: %d %q", st, body)
	}
	if got := rerank.calls(); len(got) != 2 || got[0] != "POST /rerank" || got[1] != "POST /rerank?x=1" {
		t.Fatalf("Reranker-Aufrufe: %v", got)
	}

	ollamaBefore := len(ollama.calls())
	st, hdr, body := respOf(t, runHandle(t, tun, "GET", "/rerank", ""))
	if st != http.StatusMethodNotAllowed || hdr["Allow"] != "POST" || !strings.Contains(body, "method_not_allowed") {
		t.Fatalf("GET /rerank: %d %v %q", st, hdr, body)
	}
	if len(ollama.calls()) != ollamaBefore || len(rerank.calls()) != 2 {
		t.Fatalf("405 hat einen Upstream aufgerufen: ollama %v, rerank %v", ollama.calls(), rerank.calls())
	}

	// unlisted paths pass through to Ollama; the comparison is exact
	for _, p := range []string{"/api/chat", "/rerank/", "/RERANK"} {
		st, _, body = respOf(t, runHandle(t, tun, "POST", p, `{}`))
		if st != 200 || body != ollama.reply {
			t.Fatalf("POST %s: %d %q", p, st, body)
		}
	}
	if len(rerank.calls()) != 2 {
		t.Fatalf("nicht gelisteter Pfad ging an den Reranker: %v", rerank.calls())
	}
}

func TestRoutesHealth(t *testing.T) {
	var mu sync.Mutex
	status, body := 503, `{"status":"starting","reason":"model loading"}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.URL.Path != "/health" {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(status)
		w.Write([]byte(body))
	}))
	set := func(s int, b string) { mu.Lock(); status, body = s, b; mu.Unlock() }
	r := newRoutes([]RouteCfg{{Name: "rerank", Path: "/rerank", Methods: []string{"POST"}, Upstream: srv.URL, Health: "/health"}}, quietLogger())
	if got := r.Report()["rerank"]; got != (RouteHealth{Up: false, Reason: routeStarting}) {
		t.Fatalf("vor der ersten Pruefung: %+v", got)
	}
	ctx := context.Background()
	cases := []struct {
		status int
		body   string
		want   RouteHealth
	}{
		{503, `{"status":"starting","reason":"model loading"}`, RouteHealth{Reason: routeStarting}},
		{200, `{"status":"ok","selftest":"ok"}`, RouteHealth{Up: true}},
		{503, `{"status":"selftest_failed","reason":"checksum"}`, RouteHealth{Reason: routeSelftestFailed}},
		{500, `kaputt`, RouteHealth{Reason: routeUnhealthy}},
		{503, `{"status":"irgendwas"}`, RouteHealth{Reason: routeUnhealthy}},
		{404, ``, RouteHealth{Reason: routeUnhealthy}},
	}
	for _, c := range cases {
		set(c.status, c.body)
		r.checkAll(ctx)
		if got := r.Report()["rerank"]; got != c.want {
			t.Errorf("%d %s: %+v, erwartet %+v", c.status, c.body, got, c.want)
		}
	}
	srv.Close()
	r.checkAll(ctx)
	if got := r.Report()["rerank"]; got != (RouteHealth{Reason: routeUnreachable}) {
		t.Fatalf("Dienst weg: %+v", got)
	}
	// Report is a copy
	rep := r.Report()
	rep["rerank"] = RouteHealth{Up: true}
	if r.Report()["rerank"].Up {
		t.Fatal("Report gibt die interne Map heraus")
	}
}

// Without routes, everything remains as before: no object, no Heartbeat field, Lookup finds nothing.
func TestRoutesWithoutConfiguration(t *testing.T) {
	r := newRoutes(nil, quietLogger())
	if r != nil {
		t.Fatal("newRoutes(nil) != nil")
	}
	if r.Report() != nil {
		t.Fatal("Report ohne Routen nicht nil (Heartbeat bekaeme ein leeres Feld)")
	}
	if _, ok := r.Lookup("/rerank"); ok {
		t.Fatal("Lookup ohne Routen fand etwas")
	}
	b, _ := json.Marshal(map[string]map[string]RouteHealth{"routes": {"rerank": {Up: false, Reason: routeUnreachable}}})
	if string(b) != `{"routes":{"rerank":{"up":false,"reason":"unreachable"}}}` {
		t.Fatalf("Draht-Form: %s", b)
	}
}
