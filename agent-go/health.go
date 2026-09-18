package main

import (
	"context"
	"encoding/json"
	"net/http"
	"time"
)

// HealthServer: GET /health (JSON-Gesamtbild), GET /healthz (200/503), POST /restart-child?name=stt.
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
		if h.app.sup.RestartChild(r.URL.Query().Get("name")) {
			w.Write([]byte("restart angestossen\n"))
		} else {
			w.WriteHeader(404)
			w.Write([]byte("kein laufendes Kind mit diesem Namen\n"))
		}
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
