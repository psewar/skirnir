// ollama-router-agent: Agent fuer GPU-Knoten des Ollama-Routers (Windows-Dienst, auf Linux im Vordergrund/systemd).
// Tunnel zum Router mit Schluessel-Identitaet, Heartbeat, MQTT-Geraet fuer Home Assistant, Aufsicht ueber lokale KI-Dienste.
// Verben: install | uninstall | apply-rules | start | stop | restart | status | run | identity | check-config | mqtt-clear | gpu | gpuz-relay | version
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"time"
)

var version = "dev"

func usage() {
	fmt.Fprintf(os.Stderr, `ollama-router-agent %s

  ollama-router-agent <verb> [--config <pfad>]

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
		name := "OllamaRouterAgent"
		if cfg, err := loadConfig(cfgPath); err == nil {
			name = cfg.Install.ServiceName
		}
		if err := runService(cfgPath, name); err != nil {
			os.Exit(1)
		}
		return
	}
	if verb == "" || verb == "help" || verb == "-h" || verb == "--help" {
		usage()
		os.Exit(2)
	}
	if verb == "version" {
		fmt.Println(version)
		return
	}
	if verb == "gpu" { // Messquelle pruefen: NVML oder nvidia-smi, Werte und Dauer je Messung
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
		fmt.Printf("quelle=%s name=%q util=%d%% vram=%d/%d MiB frei=%d MiB  dauer=%.2f ms je Messung (20 Messungen)\n",
			g.Source(), s.Name, s.UtilPct, s.UsedMiB, s.TotalMiB, s.FreeMiB, float64(time.Since(t0).Microseconds())/1000/20)
		if s.Sensors != nil {
			b, _ := json.MarshalIndent(s.Sensors, "", "  ")
			fmt.Printf("sensoren (gpuz=%v):\n%s\n", s.Sensors.GPUZ, b)
		}
		// GPU-Schutz, Trockenlauf: was der Guard mit den Standardwerten setzen WUERDE (setzt nichts)
		if lim := g.PowerLimits(); !lim.OK {
			fmt.Printf("gpu-guard: Limits nicht lesbar (%s) -> Zustand unverfuegbar\n", lim.Err)
		} else {
			dauer, stufe2 := newGuardEngine(GuardCfg{}, nil).targets(lim)
			fmt.Printf("gpu-guard (Trockenlauf, Standardwerte): Limit aktuell %.0f W, Standard %.0f W, erlaubt %.0f-%.0f W -> Dauerlimit %.0f W (80 %%), Stufe 2 %.0f W (70 %%)\n",
				lim.Cur, lim.Def, lim.Min, lim.Max, dauer, stufe2)
		}
		return
	}
	if verb == "gpuz-relay" { // Anmeldesitzung -> Dienst (gpuz_windows.go); Konfiguration nur fuer den Health-Port
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
		fmt.Fprintf(os.Stderr, "Konfiguration: %v\n", err)
		os.Exit(1)
	}
	var verr error
	switch verb {
	case "check-config":
		fmt.Printf("ok: node=%s router=%s tunnel=%v mqtt-lokal=%v kinder=%d identity=%s dienst=%s konto=%s\n", cfg.Node, cfg.Router.URL, cfg.Tunnel.on(),
			cfg.MQTT.Host != "", len(cfg.Children), cfg.Identity.Dir, cfg.Install.ServiceName, firstNonEmpty(cfg.Install.Account, "LocalSystem"))
	case "identity":
		log, _ := newLogger(cfg.Logging, "agent", false)
		id, err := loadOrCreateIdentity(cfg.Identity.Dir, log)
		if err != nil {
			verr = err
			break
		}
		fmt.Printf("Fingerprint: %s\nPublic Key:  %s\nKnoten:      %s\n", id.Fingerprint(), id.PublicB64(), cfg.Node)
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
			verr = fmt.Errorf("mqtt-clear braucht eine lokale mqtt-Konfiguration (host, password)")
			break
		}
		m := newMQTT(cfg.MQTT, cfg.SecretStore, cfg.Node, log, nil, nil, nil, nil)
		verr = m.clearDiscovery(context.Background())
		if verr == nil {
			fmt.Printf("Discovery fuer %s geloescht\n", cfg.MQTT.DeviceID)
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
		fmt.Printf("/health nicht erreichbar (%v)\n", err)
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var s Snapshot
	if json.Unmarshal(raw, &s) != nil {
		fmt.Println(string(raw))
		return
	}
	fmt.Printf("Version %s, node %s, laeuft seit %s, Identitaet %s\n", s.Version, s.Node, s.Uptime, s.Identity)
	if s.GPU != nil {
		fmt.Printf("GPU %s: %d %%, VRAM %d/%d MiB belegt\n", s.GPU.Name, s.GPU.UtilPct, s.GPU.UsedMiB, s.GPU.TotalMiB)
	}
	fmt.Printf("Heartbeat: ok=%v router sagt '%s' %s (gesendet %d, Fehler %d) %s\n", s.Heartbeat.OK, s.Heartbeat.State, s.Heartbeat.BusyReason, s.Heartbeat.Sent, s.Heartbeat.Failures, s.Heartbeat.LastError)
	if s.Tunnel != nil {
		fmt.Printf("Tunnel: verbunden=%v status=%s knoten=%s seit %s, Anfragen %d %s\n", s.Tunnel.Connected, s.Tunnel.State, s.Tunnel.Node, s.Tunnel.Since, s.Tunnel.Requests, s.Tunnel.LastError)
	}
	if s.MQTT != nil {
		fmt.Printf("MQTT: verbunden=%v geraet=%s veroeffentlicht=%d %s\n", s.MQTT.Connected, s.MQTT.DeviceID, s.MQTT.Published, s.MQTT.LastError)
	} else {
		fmt.Println("MQTT: aus (nicht konfiguriert / noch nicht provisioniert)")
	}
	if s.Proxy != nil {
		fmt.Printf("Ollama-TLS-Proxy: %s -> %s, sha256:%s, Anfragen %d %s\n", s.Proxy.Listen, s.Proxy.Upstream, s.Proxy.Fingerprint, s.Proxy.Requests, s.Proxy.LastError)
	}
	for name, ch := range s.Children {
		fmt.Printf("Kind %s: laeuft=%v gesund=%v pid=%d neustarts=%d %s\n", name, ch.Running, ch.Healthy, ch.PID, ch.Restarts, ch.LastExit)
	}
}
