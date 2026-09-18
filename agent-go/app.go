package main

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// App verdrahtet die Module. Laeuft identisch im Dienst und im Konsolen-Modus (run), auf Windows wie auf Linux.
type App struct {
	cfg     *Config
	log     *Logger
	gpu     *GPU
	hb      *Heartbeat
	sup     *Supervisor
	proxy   *OllamaProxy
	tunnel  *Tunnel
	id      *Identity
	started time.Time

	mu       sync.Mutex
	mq       *MQTTModule
	mqCancel context.CancelFunc
	mqCfg    *MQTTCfg
	prov     *Provision
	rootCtx  context.Context
}

func newApp(cfg *Config, log *Logger) (*App, error) {
	gpu, err := newGPU()
	if err != nil {
		return nil, err
	}
	sup, err := newSupervisor(cfg.Children, cfg.Logging, log)
	if err != nil {
		return nil, err
	}
	a := &App{cfg: cfg, log: log, gpu: gpu, sup: sup, started: time.Now()}
	a.hb = newHeartbeat(cfg.Router, cfg.Node, gpu, log)
	if cfg.OllamaProxy.on() {
		if a.proxy, err = newOllamaProxy(cfg.OllamaProxy, cfg.Router.Token, cfg.Node, log); err != nil {
			return nil, err
		}
		a.hb.tlsFP = a.proxy.fp
	}
	if cfg.Tunnel.on() {
		if a.id, err = loadOrCreateIdentity(cfg.Identity.Dir, log); err != nil {
			return nil, err
		}
		if p, err := loadProvision(cfg.Identity.Dir); err == nil {
			a.prov = p
		}
		facts := func(ctx context.Context) Facts { return collectFacts(ctx, cfg.Router.URL, cfg.Tunnel.Upstream, gpu) }
		a.tunnel = newTunnel(cfg.Router.URL, cfg.Tunnel.Upstream, a.id, a.hb, facts, log)
		a.tunnel.onStatus = a.onTunnelStatus
		if a.prov != nil && a.prov.HeartbeatIntervalS > 0 {
			a.tunnel.SetHeartbeatInterval(time.Duration(a.prov.HeartbeatIntervalS * float64(time.Second)))
		}
	}
	return a, nil
}

// effectiveMQTT: lokale Config gewinnt (explizit gesetzter Host), sonst das vom Router provisionierte Paket.
func (a *App) effectiveMQTT() *MQTTCfg {
	if a.cfg.MQTT.Enabled != nil && !*a.cfg.MQTT.Enabled {
		return nil
	}
	if a.cfg.MQTT.Host != "" {
		c := a.cfg.MQTT
		return &c
	}
	if a.prov != nil && a.prov.MQTT != nil {
		c := *a.prov.MQTT
		// Knoten-lokale Angaben ueberlagern das Provisionierte: welcher Alias hier liegt und welches Kind der STT-Server ist,
		// weiss nur dieser Rechner.
		if a.cfg.MQTT.AliasModel != "" {
			c.AliasModel = a.cfg.MQTT.AliasModel
		}
		if a.cfg.MQTT.SttChild != "" {
			c.SttChild = a.cfg.MQTT.SttChild
		}
		if a.cfg.MQTT.SttFallbackPort != 0 {
			c.SttFallbackPort = a.cfg.MQTT.SttFallbackPort
		}
		mqttDefaults(&c, a.cfg.Node)
		return &c
	}
	return nil
}

func (a *App) startMQTT(cfg *MQTTCfg) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.mqCancel != nil {
		a.mqCancel()
		a.mqCancel, a.mq = nil, nil
	}
	if cfg == nil || a.rootCtx == nil {
		a.mqCfg = nil
		return
	}
	ctx, cancel := context.WithCancel(a.rootCtx)
	a.mq = newMQTT(*cfg, a.cfg.SecretStore, a.cfg.Node, a.log, a.gpu, a.hb, a.sup)
	a.mqCancel, a.mqCfg = cancel, cfg
	go a.mq.Run(ctx)
}

// onTunnelStatus: Freigabe/Sperre/Konfigurationspaket vom Router.
func (a *App) onTunnelStatus(state, node string, p *Provision) {
	if p == nil {
		if state == "revoked" {
			a.startMQTT(nil)
		}
		return
	}
	if node != "" {
		p.Node = node
	}
	a.mu.Lock()
	changed := a.prov == nil || !a.prov.equal(p)
	a.prov = p
	a.mu.Unlock()
	if !changed {
		return
	}
	if err := saveProvision(a.cfg.Identity.Dir, p); err != nil {
		a.log.Warnf("provision: speichern: %v", err)
	} else {
		a.log.Infof("provision: Konfigurationspaket vom Router uebernommen (mqtt=%v, heartbeat %.0fs)", p.MQTT != nil, p.HeartbeatIntervalS)
	}
	if p.HeartbeatIntervalS > 0 && a.tunnel != nil {
		a.tunnel.SetHeartbeatInterval(time.Duration(p.HeartbeatIntervalS * float64(time.Second)))
	}
	a.startMQTT(a.effectiveMQTT())
}

// Run blockiert bis ctx endet; dann werden alle Module gestoppt (Kinder inklusive).
func (a *App) Run(ctx context.Context) error {
	a.rootCtx = ctx
	fp := ""
	if a.id != nil {
		fp = a.id.Fingerprint()[:16]
	}
	a.log.Infof("ollama-router-agent %s startet: node=%s gpu=%s (%s) kinder=%d tunnel=%v identity=%s", version, a.cfg.Node, a.gpu.Source(), a.gpu.smi, len(a.cfg.Children), a.tunnel != nil, fp)
	var wg sync.WaitGroup
	run := func(name string, f func(context.Context)) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() {
				if r := recover(); r != nil {
					a.log.Errorf("%s: panic: %v", name, r)
				}
			}()
			f(ctx)
		}()
	}
	if a.tunnel == nil {
		run("heartbeat", a.hb.Run) // alter HTTP-Weg
	}
	run("supervisor", a.sup.Run)
	run("health", (&HealthServer{app: a}).Run)
	if a.proxy != nil {
		run("ollama-proxy", a.proxy.Run)
	}
	if a.tunnel != nil {
		run("tunnel", a.tunnel.Run)
	}
	a.startMQTT(a.effectiveMQTT())
	<-ctx.Done()
	a.log.Infof("stoppe Module ...")
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		return fmt.Errorf("Module haben nach 20 s nicht gestoppt")
	}
	a.log.Infof("gestoppt")
	return nil
}

func (a *App) mqStatus() *MQTTStatus {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.mq == nil {
		return nil
	}
	st := a.mq.Status()
	return &st
}
