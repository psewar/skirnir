package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
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
	guard    *Guard   // GPU-Schutz (guard.go)
	updater  *Updater // Selbst-Update (updater.go)
	rootCtx  context.Context
	crashed  string // Modul, das mit Panic endete (Run liefert dann einen Fehler, der Dienst startet neu)
	ctlToken string // Token fuer POST /restart-child (Datei control.token neben der Config; Review 2026-09-25)
}

func newApp(cfg *Config, log *Logger) (*App, error) {
	gpu, err := newGPU()
	if err != nil {
		return nil, err
	}
	if cfg.GPUZ.on() {
		gpu.enableGPUZ(log)
	}
	sup, err := newSupervisor(cfg.Children, cfg.Logging, log)
	if err != nil {
		return nil, err
	}
	a := &App{cfg: cfg, log: log, gpu: gpu, sup: sup, started: time.Now()}
	a.ctlToken = loadOrCreateControlToken(cfg.path, log)
	a.hb = newHeartbeat(cfg.Node, gpu, log)
	a.guard = newGuard(cfg.GPUGuard, gpu, log)
	a.hb.guard = a.guard
	a.updater = newUpdater(cfg.Update, cfg.path, log)
	a.hb.updater = a.updater
	a.updater.Cleanup()
	if cfg.OllamaProxy.on() {
		if a.proxy, err = newOllamaProxy(cfg.OllamaProxy, cfg.Router.Token, cfg.Node, log); err != nil {
			return nil, err
		}
		a.hb.tlsFP = a.proxy.fp
	}
	if a.id, err = loadOrCreateIdentity(cfg.Identity.Dir, log); err != nil {
		return nil, err
	}
	if p, err := loadProvision(cfg.Identity.Dir); err == nil {
		a.prov = p
	}
	facts := func(ctx context.Context) Facts { return collectFacts(ctx, cfg.Router.URL, cfg.Tunnel.Upstream, gpu) }
	a.tunnel = newTunnel(cfg.Router.URL, cfg.Tunnel.Upstream, a.id, a.hb, facts, log)
	a.tunnel.onStatus = a.onTunnelStatus
	a.tunnel.onUpdate = a.updater.Handle
	if a.prov != nil && a.prov.HeartbeatIntervalS > 0 {
		a.tunnel.SetHeartbeatInterval(time.Duration(a.prov.HeartbeatIntervalS * float64(time.Second)))
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
	a.mu.Lock()
	prov := a.prov
	a.mu.Unlock()
	if prov != nil && prov.MQTT != nil {
		c := *prov.MQTT
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
	a.mq = newMQTT(*cfg, a.cfg.SecretStore, a.cfg.Node, a.log, a.gpu, a.hb, a.sup, a.guard)
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
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	a.rootCtx = ctx
	fp := ""
	if a.id != nil {
		fp = a.id.Fingerprint()[:16]
	}
	a.log.Infof("skirnir-agent %s startet: node=%s gpu=%s (%s) kinder=%d tunnel=%v identity=%s", version, a.cfg.Node, a.gpu.Source(), a.gpu.smi, len(a.cfg.Children), a.tunnel != nil, fp)
	var wg sync.WaitGroup
	run := func(name string, f func(context.Context)) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() {
				if r := recover(); r != nil {
					// Nicht still weiterlaufen: ohne das Modul (z. B. Tunnel) waere der Dienst ein gesunder Zombie, den kein
					// Recovery neu startet. Alles stoppen, Run liefert einen Fehler, der SCM/systemd startet neu.
					a.log.Errorf("%s: panic: %v - Dienst wird beendet", name, r)
					a.mu.Lock()
					if a.crashed == "" {
						a.crashed = name
					}
					a.mu.Unlock()
					cancel()
				}
			}()
			f(ctx)
		}()
	}
	run("supervisor", a.sup.Run)
	run("gpu-guard", a.guard.Run)
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
	a.mu.Lock()
	crashed := a.crashed
	a.mu.Unlock()
	if crashed != "" {
		return fmt.Errorf("Modul %s ist abgestuerzt (panic)", crashed)
	}
	return nil
}

// loadOrCreateControlToken: zufaelliges Token in control.token neben der Config (Verzeichnis-ACL: SYSTEM, Administratoren,
// Dienstkonto). Wer /restart-child aufrufen will, muss es lesen koennen. Ohne Config-Pfad (Tests) nur im Speicher.
func loadOrCreateControlToken(cfgPath string, log *Logger) string {
	fresh := func() string {
		b := make([]byte, 24)
		_, _ = rand.Read(b)
		return hex.EncodeToString(b)
	}
	if cfgPath == "" {
		return fresh()
	}
	p := filepath.Join(filepath.Dir(cfgPath), "control.token")
	if raw, err := os.ReadFile(p); err == nil && len(strings.TrimSpace(string(raw))) >= 32 {
		return strings.TrimSpace(string(raw))
	}
	tok := fresh()
	if err := os.WriteFile(p, []byte(tok+"\n"), 0o600); err != nil {
		log.Warnf("control.token: %v (Token nur im Speicher)", err)
	}
	return tok
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
