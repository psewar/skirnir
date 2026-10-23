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

// App wires up the modules. Runs identically as a service and in console mode (run), on Windows and Linux.
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
	guard    *Guard         // GPU-Schutz (guard.go)
	updater  *Updater       // Selbst-Update (updater.go)
	ollamaUp *OllamaUpdater // Ollama update via the router (ollamaupdate.go, 0.12.0)
	ollamaW  *OllamaWatcher // Monitor and report Ollama status (ollamawatch.go, 0.14.0)
	upHold   *upstreamHold  // Stop/start an OpenAI-compatible upstream on router order (upstream_hold.go, 0.22.0); nil = not configured
	game     *GameWatch     // Spielerkennung (game.go, 0.17.0)
	routes   *Routes        // Tunnel routes to other local services (routes.go); nil without tunnel.routes
	rootCtx  context.Context
	crashed  string // Module that ended with a panic (Run then returns an error, the service restarts)
	ctlToken string // Token for POST /restart-child (file control.token next to the Config; Review 2026-09-25)
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
	a.hb.sup = sup
	a.game = newGameWatch(log)
	a.hb.game = a.game
	a.guard = newGuard(cfg.GPUGuard, gpu, log)
	a.hb.guard = a.guard
	a.updater = newUpdater(cfg.Update, cfg.path, log)
	a.hb.updater = a.updater
	a.updater.Cleanup()
	updSpecs := cfg.Children
	if fb := cfg.Tunnel.UpstreamFallback; fb != nil {
		// The standby Ollama runs only while the upstream is held: parked from the start, and not the updater's to stop
		// and start (an update would start it next to the upstream and then check a version it cannot get).
		sup.Park(fb.Child)
		updSpecs = withoutChild(cfg.Children, fb.Child)
	}
	a.ollamaUp = newOllamaUpdater(cfg.OllamaUpdate, updSpecs, sup, cfg.Tunnel.Upstream, log)
	a.hb.ollamaUp, a.hb.upstream = a.ollamaUp, cfg.Tunnel.Upstream
	if cfg.Tunnel.UpstreamAPI == upstreamOpenAI {
		a.hb.upstream = "" // no Ollama version to report (heartbeat asks /api/version only with an upstream)
	}
	if a.ollamaUp.Managed() {
		log.Infof("ollama-update: Ollama is child %q in %s - router may trigger updates", a.ollamaUp.child, a.ollamaUp.dir)
	}
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
	facts := func(ctx context.Context) Facts {
		f := collectFacts(ctx, cfg.Router.URL, cfg.Tunnel.Upstream, cfg.Tunnel.UpstreamAPI, gpu)
		m := a.ollamaUp.Managed()
		f.OllamaManaged = &m
		f.OllamaPush = a.ollamaW != nil
		f.OllamaSwapGate = a.ollamaUp.SwapGate()
		f.UpstreamRelease = a.upHold != nil
		f.UpstreamFallback = a.upHold != nil && a.upHold.fbChild != ""
		return f
	}
	a.tunnel = newTunnel(cfg.Router.URL, cfg.Tunnel.Upstream, a.id, a.hb, facts, log)
	a.tunnel.onStatus = a.onTunnelStatus
	a.tunnel.onUpdate = a.updater.Handle
	a.tunnel.onOllamaUpdate = a.ollamaUp.Handle
	a.tunnel.onOllamaSwap = a.ollamaUp.Swap
	a.ollamaW = newOllamaWatcher(cfg.Tunnel.Upstream, cfg.Tunnel.UpstreamAPI, log)
	a.tunnel.ollama = a.ollamaW
	a.tunnel.upSwitch = newUpstreamSwitch(cfg.Tunnel.UpstreamFallback)
	a.upHold = newUpstreamHold(sup, cfg.Tunnel.UpstreamChild, cfg.Tunnel.UpstreamFallback, a.tunnel.upSwitch, a.ollamaW, log)
	a.tunnel.onUpstream = a.upHold.Handle
	a.routes = newRoutes(cfg.Tunnel.Routes, log)
	a.tunnel.routes, a.hb.routes = a.routes, a.routes
	if a.prov != nil && a.prov.HeartbeatIntervalS > 0 {
		a.tunnel.SetHeartbeatInterval(time.Duration(a.prov.HeartbeatIntervalS * float64(time.Second)))
	}
	return a, nil
}

// effectiveMQTT: local Config wins (explicitly set Host), otherwise the package provisioned by the router.
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
		// Node-local settings override the provisioned ones: which alias is here and which child is the STT server,
		// only this machine knows.
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
	if a.cfg.Tunnel.UpstreamAPI == upstreamOpenAI { // no /api/ps there: the Ollama entities come from the watcher
		a.mq.upstream, a.mq.upstreamChild = a.ollamaW, a.cfg.Tunnel.UpstreamChild
	}
	a.mqCancel, a.mqCfg = cancel, cfg
	go a.mq.Run(ctx)
}

// onTunnelStatus: Release/Block/Config package from the router.
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
		a.log.Warnf("provision: save: %v", err)
	} else {
		a.log.Infof("provision: configuration package from the router applied (mqtt=%v, heartbeat %.0fs)", p.MQTT != nil, p.HeartbeatIntervalS)
	}
	if p.HeartbeatIntervalS > 0 && a.tunnel != nil {
		a.tunnel.SetHeartbeatInterval(time.Duration(p.HeartbeatIntervalS * float64(time.Second)))
	}
	a.startMQTT(a.effectiveMQTT())
}

// Run blocks until ctx ends; then all modules are stopped (including children).
func (a *App) Run(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	a.rootCtx = ctx
	fp := ""
	if a.id != nil {
		fp = a.id.Fingerprint()[:16]
	}
	a.log.Infof("skirnir-agent %s starting: node=%s gpu=%s (%s) children=%d tunnel=%v identity=%s", version, a.cfg.Node, a.gpu.Source(), a.gpu.smi, len(a.cfg.Children), a.tunnel != nil, fp)
	var wg sync.WaitGroup
	run := func(name string, f func(context.Context)) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() {
				if r := recover(); r != nil {
					// Do not keep running silently: without the module (e.g. Tunnel) the service would be a healthy zombie that no
					// recovery restarts. Stop everything, Run returns an error, the SCM/systemd restarts.
					a.log.Errorf("%s: panic: %v - service is stopping", name, r)
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
	if a.ollamaW != nil {
		run("ollama-watch", a.ollamaW.Run)
	}
	if a.routes != nil {
		run("tunnel-routen", a.routes.Run)
	}
	run("ollama-registrierung", a.ollamaUp.RegistrationLoop) // Sync the Windows installation entry with the running version
	run("gpu-guard", a.guard.Run)
	run("game", a.game.Run)
	run("health", (&HealthServer{app: a}).Run)
	if a.proxy != nil {
		run("ollama-proxy", a.proxy.Run)
	}
	if a.tunnel != nil {
		run("tunnel", a.tunnel.Run)
	}
	a.startMQTT(a.effectiveMQTT())
	<-ctx.Done()
	a.log.Infof("stopping modules ...")
	done := make(chan struct{})
	go func() { wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		return fmt.Errorf("modules did not stop within 20 s")
	}
	a.log.Infof("stopped")
	a.mu.Lock()
	crashed := a.crashed
	a.mu.Unlock()
	if crashed != "" {
		return fmt.Errorf("module %s crashed (panic)", crashed)
	}
	return nil
}

// loadOrCreateControlToken: random token in control.token next to the Config (Directory ACL: SYSTEM, Administrators,
// Service account). Whoever wants to call /restart-child must be able to read it. Without a Config path (tests) only in memory.
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
		log.Warnf("control.token: %v (token in memory only)", err)
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
