// skirnir-agent: Agent for GPU nodes of the Skirnir router (Windows service, foreground/systemd on Linux).
// Tunnel to the router with key identity, heartbeat, MQTT device for Home Assistant, supervision of local AI services.
// Verben: setup | install | uninstall | apply-rules | start | stop | restart | status | run | identity | check-config | mqtt-clear | gpu | gpuz-relay | version
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"sort"
	"strings"
	"time"
)

var version = "dev"

func usage() {
	fmt.Fprintf(os.Stderr, `skirnir-agent %s

  skirnir-agent <verb> [--config <pfad>]

  setup         Einrichtung per Doppelklick (Windows): fragt nach Adminrechten, legt Konfiguration und Dienst an oder
				aktualisiert beide, GPU-Z-Relay-Aufgabe, Zustand - Standard, wenn kein Verb angegeben ist
  install       Dienst anlegen (Windows, Admin): Konto, Recovery, ACLs, Firewall, Steuerrecht
  uninstall     Dienst entfernen (Windows, Admin)
  apply-rules   ACLs, Firewall-Regeln und Steuerrecht neu setzen (Windows, Admin)
  start|stop|restart   Dienst steuern (Windows; ohne Admin, wenn install das Steuerrecht gesetzt hat)
  status        Dienstzustand + /health des laufenden Agenten
  run           im Vordergrund laufen (Konsole/systemd, Ctrl-C beendet)
  identity      Fingerprint und Public Key dieses Agenten zeigen (zum Abgleich mit der Router-UI)
  check-config  Konfiguration laden und pruefen
  mqtt-clear    retained Discovery-Configs dieses Geraets loeschen (Testgeraete aufraeumen)
  gpu           Messquelle (NVML/nvidia-smi) und alle Sensoren einmal ausgeben, GPU-Z eingeschlossen
  gpuz-relay    GPU-Z-Sensoren aus der Anmeldesitzung an den Dienst reichen (Windows; Aufgabe bei Anmeldung)
  spiel         Spielerkennung und Hauptspeicher einmal ausgeben (Spielordner, Programmnamen, Treffer)
  version

  Standard-Config: %s
`, version, defaultConfigPath)
}

func main() {
	cfgPath := defaultConfigPath
	var verb string
	args := os.Args[1:]
	for i := 0; i < len(args); i++ {
		switch {
		case args[i] == "--config" && i+1 < len(args):
			cfgPath = args[i+1]
			i++
		case strings.HasPrefix(args[i], "--config="):
			cfgPath = strings.TrimPrefix(args[i], "--config=")
		case verb == "":
			verb = args[i]
		}
	}
	if runningAsService() {
		name := "SkirnirAgent"
		if cfg, err := loadConfig(cfgPath); err == nil {
			name = cfg.Install.ServiceName
		}
		if err := runService(cfgPath, name); err != nil {
			os.Exit(1)
		}
		return
	}
	if verb == "" && runtime.GOOS == "windows" {
		verb = "setup" // Double-click the exe
	}
	if verb == "" || verb == "help" || verb == "-h" || verb == "--help" {
		usage()
		os.Exit(2)
	}
	if verb == "setup" { // requires no readable configuration - creates it if needed
		if err := setupInteractive(cfgPath); err != nil {
			fmt.Fprintf(os.Stderr, "setup: %v\n", err)
			os.Exit(1)
		}
		return
	}
	if verb == "version" {
		fmt.Println(version)
		return
	}
	if verb == "gpu" { // Check measurement source: NVML or nvidia-smi, values and duration per measurement
		g, err := newGPU()
		if err != nil {
			fmt.Fprintf(os.Stderr, "gpu: %v\n", err)
			os.Exit(1)
		}
		g.enableGPUZ(nil)
		t0 := time.Now()
		var s *GPUSample
		for i := 0; i < 20; i++ {
			if s, err = g.Sample(context.Background()); err != nil {
				fmt.Fprintf(os.Stderr, "gpu: %v\n", err)
				os.Exit(1)
			}
		}
		fmt.Printf("source=%s name=%q util=%d%% vram=%d/%d MiB free=%d MiB  duration=%.2f ms per sample (20 samples)\n",
			g.Source(), s.Name, s.UtilPct, s.UsedMiB, s.TotalMiB, s.FreeMiB, float64(time.Since(t0).Microseconds())/1000/20)
		if s.Sensors != nil {
			b, _ := json.MarshalIndent(s.Sensors, "", "  ")
			fmt.Printf("sensors (gpuz=%v):\n%s\n", s.Sensors.GPUZ, b)
		}
		// 0.13.0: Graphics memory per process (performance indicator) - basis for children_vram_mib in the heartbeat
		if procVRAM, err := gpuProcessVRAM(); err != nil {
			fmt.Printf("graphics memory per process: %v\n", err)
		} else {
			printGPUProcs(procVRAM)
		}
		// GPU guard, dry run: what the guard WOULD set with default values (sets nothing)
		if lim := g.PowerLimits(); !lim.OK {
			fmt.Printf("gpu-guard: limits not readable (%s) -> state unavailable\n", lim.Err)
		} else {
			sustained, stage2 := newGuardEngine(GuardCfg{}, nil).targets(lim)
			fmt.Printf("gpu-guard (dry run, defaults): limit now %.0f W, default %.0f W, allowed %.0f-%.0f W -> sustained limit %.0f W (80 %%), stage 2 %.0f W (70 %%)\n",
				lim.Cur, lim.Def, lim.Min, lim.Max, sustained, stage2)
		}
		return
	}
	if verb == "game" { // 0.17.0: same detection as the module, once - without configuration (until 0.19: "game")
		probeGame()
		return
	}
	if verb == "gpuz-relay" { // Login session -> service (gpuz_windows.go); configuration only for the health port
		listen := "127.0.0.1:10398"
		if cfg, err := loadConfig(cfgPath); err == nil {
			listen = cfg.Health.Listen
		}
		if err := runGPUZRelay(listen); err != nil {
			fmt.Fprintf(os.Stderr, "gpuz-relay: %v\n", err)
			os.Exit(1)
		}
		return
	}
	cfg, err := loadConfig(cfgPath)
	if err != nil {
		// Controlling the service and showing status works without the config: since 0.9.0, controlling users are allowed to do this
		// No longer read the config directory (only the service control right) - default names suffice for these verbs.
		if (os.IsPermission(err) || os.IsNotExist(err)) && (verb == "status" || verb == "start" || verb == "stop" || verb == "restart") {
			fmt.Fprintf(os.Stderr, "(configuration not readable - defaults: service SkirnirAgent, health 127.0.0.1:10398)\n")
			cfg = &Config{}
			cfg.applyDefaults()
		} else {
			fmt.Fprintf(os.Stderr, "configuration: %v\n", err)
			os.Exit(1)
		}
	}
	var verr error
	switch verb {
	case "check-config":
		fmt.Printf("ok: node=%s router=%s upstream=%s mqtt-local=%v children=%d identity=%s service=%s account=%s\n", cfg.Node, cfg.Router.URL, cfg.Tunnel.Upstream,
			cfg.MQTT.Host != "", len(cfg.Children), cfg.Identity.Dir, cfg.Install.ServiceName, firstNonEmpty(cfg.Install.Account, "LocalSystem"))
	case "identity":
		log, _ := newLogger(cfg.Logging, "agent", false)
		id, err := loadOrCreateIdentity(cfg.Identity.Dir, log)
		if err != nil {
			verr = err
			break
		}
		fmt.Printf("Fingerprint: %s\nPublic key:  %s\nNode:        %s\n", id.Fingerprint(), id.PublicB64(), cfg.Node)
	case "run":
		verr = runConsole(cfg)
	case "install":
		verr = install(cfg, cfgPath)
	case "uninstall":
		verr = uninstall(cfg)
	case "apply-rules":
		verr = applyRules(cfg, cfgPath)
	case "start", "stop", "restart":
		verr = controlService(cfg.Install.ServiceName, verb)
		if verr == nil {
			st, _ := serviceState(cfg.Install.ServiceName)
			fmt.Println(cfg.Install.ServiceName + ": " + st)
		}
	case "status":
		st, err := serviceState(cfg.Install.ServiceName)
		if err != nil {
			verr = err
			break
		}
		fmt.Println(cfg.Install.ServiceName + ": " + st)
		printHealth(cfg.Health.Listen)
	case "mqtt-clear":
		log, _ := newLogger(cfg.Logging, "agent", true)
		if cfg.MQTT.Host == "" {
			verr = fmt.Errorf("mqtt-clear needs a local mqtt configuration (host, password)")
			break
		}
		m := newMQTT(cfg.MQTT, cfg.SecretStore, cfg.Node, log, nil, nil, nil, nil)
		verr = m.clearDiscovery(context.Background())
		if verr == nil {
			fmt.Printf("discovery for %s deleted\n", cfg.MQTT.DeviceID)
		}
	default:
		usage()
		os.Exit(2)
	}
	if verr != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", verb, verr)
		os.Exit(1)
	}
}

func runConsole(cfg *Config) error {
	log, err := newLogger(cfg.Logging, "agent", true)
	if err != nil {
		return err
	}
	app, err := newApp(cfg, log)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	return app.Run(ctx)
}

func printHealth(listen string) {
	c := &http.Client{Timeout: 3 * time.Second}
	resp, err := c.Get("http://" + listen + "/health")
	if err != nil {
		fmt.Printf("/health unreachable (%v)\n", err)
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var s Snapshot
	if json.Unmarshal(raw, &s) != nil {
		fmt.Println(string(raw))
		return
	}
	fmt.Printf("version %s, node %s, running for %s, identity %s\n", s.Version, s.Node, s.Uptime, s.Identity)
	if s.GPU != nil {
		fmt.Printf("GPU %s: %d %%, VRAM %d/%d MiB used\n", s.GPU.Name, s.GPU.UtilPct, s.GPU.UsedMiB, s.GPU.TotalMiB)
	}
	fmt.Printf("heartbeat: ok=%v router says '%s' %s (sent %d, failures %d) %s\n", s.Heartbeat.OK, s.Heartbeat.State, s.Heartbeat.BusyReason, s.Heartbeat.Sent, s.Heartbeat.Failures, s.Heartbeat.LastError)
	if s.Tunnel != nil {
		fmt.Printf("tunnel: connected=%v status=%s node=%s since %s, requests %d %s\n", s.Tunnel.Connected, s.Tunnel.State, s.Tunnel.Node, s.Tunnel.Since, s.Tunnel.Requests, s.Tunnel.LastError)
	}
	if s.MQTT != nil {
		fmt.Printf("MQTT: connected=%v device=%s published=%d %s\n", s.MQTT.Connected, s.MQTT.DeviceID, s.MQTT.Published, s.MQTT.LastError)
	} else {
		fmt.Println("MQTT: off (not configured / not provisioned yet)")
	}
	if s.Proxy != nil {
		fmt.Printf("Ollama TLS proxy: %s -> %s, sha256:%s, requests %d %s\n", s.Proxy.Listen, s.Proxy.Upstream, s.Proxy.Fingerprint, s.Proxy.Requests, s.Proxy.LastError)
	}
	for name, ch := range s.Children {
		fmt.Printf("child %s: running=%v healthy=%v pid=%d restarts=%d %s\n", name, ch.Running, ch.Healthy, ch.PID, ch.Restarts, ch.LastExit)
	}
}

// printGPUProcs: largest allocations (>= 50 MiB) descending, for the `gpu` diagnostic command.
func printGPUProcs(procVRAM map[uint32]uint64) {
	type e struct {
		pid uint32
		mib uint64
	}
	var l []e
	for pid, b := range procVRAM {
		if b>>20 >= 50 {
			l = append(l, e{pid, b >> 20})
		}
	}
	sort.Slice(l, func(i, j int) bool { return l[i].mib > l[j].mib })
	fmt.Printf("graphics memory per process (from 50 MiB, %d processes with GPU memory):\n", len(procVRAM))
	for _, x := range l {
		fmt.Printf("  pid %-7d %6d MiB\n", x.pid, x.mib)
	}
}
