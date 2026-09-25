package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"
)

// HealthServer: GET /health (JSON-Gesamtbild), GET /healthz (200/503), POST /restart-child?name=stt (Header
// X-Agent-Token = Inhalt von control.token neben der Config), POST /gpuz (GPU-Z-Sensoren vom Relay aus der
// Anmeldesitzung, nur von localhost; die Werte gehen in Sensoren und Warnungen, nie in die Limit-Entscheidung des Guards).
// Nur auf localhost; dient Debugging, dem status-Verb und einem etwaigen externen Watchdog.
type HealthServer struct {
	app *App
}

func (h *HealthServer) Run(ctx context.Context) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(h.app.snapshot())
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if h.app.hb.Status().OK {
			w.WriteHeader(200)
			w.Write([]byte("ok\n"))
			return
		}
		w.WriteHeader(503)
		w.Write([]byte("heartbeat stale\n"))
	})
	mux.HandleFunc("/restart-child", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(405)
			return
		}
		if h.app.ctlToken == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Agent-Token")), []byte(h.app.ctlToken)) != 1 {
			w.WriteHeader(403)
			w.Write([]byte("X-Agent-Token fehlt oder falsch (control.token neben der Config)\n"))
			return
		}
		if h.app.sup.RestartChild(r.URL.Query().Get("name")) {
			w.Write([]byte("restart angestossen\n"))
		} else {
			w.WriteHeader(404)
			w.Write([]byte("kein laufendes Kind mit diesem Namen\n"))
		}
	})
	mux.HandleFunc("/gpuz", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(405)
			return
		}
		host := r.RemoteAddr
		if i := strings.LastIndex(host, ":"); i > 0 {
			host = host[:i]
		}
		if host != "127.0.0.1" && host != "[::1]" {
			w.WriteHeader(403)
			return
		}
		var s GPUSensors
		if err := json.NewDecoder(io.LimitReader(r.Body, 64<<10)).Decode(&s); err != nil {
			w.WriteHeader(400)
			w.Write([]byte("json: " + err.Error() + "\n"))
			return
		}
		if !h.app.gpu.SetRelayed(&s) {
			w.WriteHeader(409)
			w.Write([]byte("gpuz.enabled: false\n"))
			return
		}
		w.Write([]byte("ok\n"))
	})
	srv := &http.Server{Addr: h.app.cfg.Health.Listen, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		<-ctx.Done()
		c, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		srv.Shutdown(c)
	}()
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		h.app.log.Errorf("health: %v", err)
	}
}

type Snapshot struct {
	Version   string                `json:"version"`
	Node      string                `json:"node"`
	Identity  string                `json:"identity,omitempty"`
	Uptime    string                `json:"uptime"`
	GPU       *GPUSample            `json:"gpu"`
	GPUError  string                `json:"gpu_error,omitempty"`
	GPUZRelay string                `json:"gpuz_relay_age,omitempty"` // Alter der letzten Relay-Werte (leer = nie)
	Guard     GuardStatus           `json:"gpu_guard"`
	Update    *UpdateReport         `json:"update,omitempty"`
	Heartbeat HeartbeatStatus       `json:"heartbeat"`
	MQTT      *MQTTStatus           `json:"mqtt,omitempty"`
	Proxy     *ProxyStatus          `json:"ollama_proxy,omitempty"`
	Tunnel    *TunnelStatus         `json:"tunnel,omitempty"`
	Children  map[string]childState `json:"children"`
}

func (a *App) snapshot() Snapshot {
	s := Snapshot{Version: version, Node: a.cfg.Node, Uptime: time.Since(a.started).Round(time.Second).String(),
		Heartbeat: a.hb.Status(), Children: a.sup.Status()}
	s.GPU, _ = a.gpu.Last()
	if _, err := a.gpu.Last(); err != nil {
		s.GPUError = err.Error()
	}
	s.Guard = a.guard.Status()
	s.Update = a.updater.Report()
	if age := a.gpu.RelayAge(); age > 0 {
		s.GPUZRelay = age.Round(time.Second).String()
	}
	s.MQTT = a.mqStatus()
	if a.id != nil {
		s.Identity = a.id.Fingerprint()
	}
	if a.proxy != nil {
		st := a.proxy.Status()
		s.Proxy = &st
	}
	if a.tunnel != nil {
		st := a.tunnel.Status()
		s.Tunnel = &st
	}
	return s
}
