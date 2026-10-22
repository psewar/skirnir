package main

import (
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"

	"gopkg.in/yaml.v3"
)

// Config ist die eine lokale Konfigurationsdatei des Agenten (YAML). Seit dem Tunnel mit Schluessel-Identitaet
// braucht ein Knoten nur noch router.url; Name, GPU, MAC meldet der Agent selbst, MQTT-Zugang und Intervalle
// provisioniert der Router nach der Freigabe. Lokale Angaben hier ueberschreiben das Provisionierte.
type Config struct {
	Node         string `yaml:"node"` // leer = Hostname (klein)
	Router       RouterCfg
	MQTT         MQTTCfg                 // optional: lokale Ueberschreibung (Host gesetzt) oder enabled: false
	SecretStore  SecretStoreCfg          `yaml:"secret_store"` // optional: nur fuer mqtt.password_secret (Universal-Auth-API)
	Health       struct{ Listen string } `yaml:"health"`
	Logging      LogCfg
	Children     []ChildSpec `yaml:"children"`
	Install      InstallCfg
	OllamaProxy  ProxyCfg        `yaml:"ollama_proxy"`
	Tunnel       TunnelCfg       `yaml:"tunnel"`
	GPUZ         GPUZCfg         `yaml:"gpuz"`          // GPU-Z-Sensoren als Zusatzquelle (Windows), Standard an
	GPUGuard     GuardCfg        `yaml:"gpu_guard"`     // GPU-Schutz: Power-Limit, Hochlast-Stufe, Warnungen (guard.go), Standard an
	Update       UpdateCfg       `yaml:"update"`        // Selbst-Update ueber den Router (updater.go): public_key des Betreibers, enabled
	OllamaUpdate OllamaUpdateCfg `yaml:"ollama_update"` // Ollama-Update ueber den Router (ollamaupdate.go, 0.12.0): enabled, source
	path         string          // Pfad der geladenen Datei (fuer den Neustart nach einem Update)
	Identity     struct {
		Dir string `yaml:"dir"`
	} `yaml:"identity"`
}

// TunnelCfg: ausgehende Dauerverbindung zum Router, durch die der Router Ollama aufruft. Seit 0.9.1 der einzige Weg
// (der HTTP-Heartbeat mit Token und `tunnel.enabled` sind weg).
type TunnelCfg struct {
	Upstream string     `yaml:"upstream"`
	Routes   []RouteCfg `yaml:"routes"` // optional: weitere lokale Dienste nach Pfad (routes.go); leer = alles an upstream
}

// RouteCfg: ein Pfad, den der Tunnel statt an Ollama an einen anderen lokalen Dienst gibt (z. B. den Reranker).
type RouteCfg struct {
	Name     string   `yaml:"name"`     // Schluessel im Heartbeat-Feld routes
	Path     string   `yaml:"path"`     // exakter Vergleich mit dem Pfad der Anfrage (ohne Query)
	Methods  []string `yaml:"methods"`  // erlaubte Methoden; andere bekommen 405
	Upstream string   `yaml:"upstream"` // nur Loopback: http://127.0.0.1:<port> oder http://[::1]:<port>
	Health   string   `yaml:"health"`   // Pfad der Gesundheitspruefung am Upstream, alle 5 s
}

// GPUZCfg: GPU-Z-Shared-Memory als Zusatzquelle (Speichertemperatur, Hot Spot, Spannung, 16-Pin-Leistung). Standard an;
// laeuft GPU-Z nicht, passiert nichts. `enabled: false` schaltet den Leser ganz ab.
type GPUZCfg struct {
	Enabled *bool `yaml:"enabled"`
}

func (g *GPUZCfg) on() bool { return g.Enabled == nil || *g.Enabled }

// ProxyCfg: TLS-Vorschaltstelle vor dem lokalen Ollama (Opt-in, Alternative ohne Tunnel).
type ProxyCfg struct {
	Enabled      *bool  `yaml:"enabled"`
	Listen       string `yaml:"listen"`
	Upstream     string `yaml:"upstream"`
	CertDir      string `yaml:"cert_dir"`
	RequireToken *bool  `yaml:"require_token"`
}

func (p *ProxyCfg) on() bool           { return p.Enabled != nil && *p.Enabled }
func (p *ProxyCfg) requireToken() bool { return p.RequireToken == nil || *p.RequireToken }

type RouterCfg struct {
	URL   string `yaml:"url"`
	Token string `yaml:"token"` // nur fuer die TLS-Vorschaltstelle (ollama_proxy: X-Router-Token)
}

// MQTTCfg wird sowohl lokal (YAML) als auch vom Router provisioniert (JSON) gelesen.
type MQTTCfg struct {
	Enabled         *bool   `yaml:"enabled" json:"enabled,omitempty"`
	Host            string  `yaml:"host" json:"host"`
	Port            int     `yaml:"port" json:"port"`
	TLS             bool    `yaml:"tls" json:"tls"`
	Username        string  `yaml:"username" json:"username"`
	Password        string  `yaml:"password" json:"password"`
	PasswordSecret  string  `yaml:"password_secret" json:"-"` // Key in Secret-Store (nur lokal)
	DeviceID        string  `yaml:"device_id" json:"device_id"`
	DeviceName      string  `yaml:"device_name" json:"device_name"`
	Manufacturer    string  `yaml:"manufacturer" json:"manufacturer"`
	Model           string  `yaml:"model" json:"model"`
	DiscoveryPrefix string  `yaml:"discovery_prefix" json:"discovery_prefix"`
	IntervalS       float64 `yaml:"interval_s" json:"interval_s"`
	OllamaURL       string  `yaml:"ollama_url" json:"ollama_url"`
	AliasModel      string  `yaml:"alias_model" json:"alias_model"`
	SttChild        string  `yaml:"stt_child" json:"stt_child"`
	SttFallbackPort int     `yaml:"stt_fallback_port" json:"stt_fallback_port"`
}

// SecretStoreCfg: Zugang zu einem Secret-Store mit Universal-Auth-API (Client-ID/-Secret -> Token -> Secret je Schluessel).
type SecretStoreCfg struct {
	Domain       string `yaml:"domain"`
	ProjectID    string `yaml:"project_id"`
	Environment  string `yaml:"environment"`
	SecretPath   string `yaml:"secret_path"`
	ClientID     string `yaml:"client_id"`
	ClientSecret string `yaml:"client_secret"`
}

type LogCfg struct {
	Dir        string `yaml:"dir"`
	MaxSizeMB  int    `yaml:"max_size_mb"`
	MaxBackups int    `yaml:"max_backups"`
	Debug      bool   `yaml:"debug"`
}

// ChildSpec beschreibt einen ueberwachten Kindprozess (z. B. den Wyoming-STT-Server).
type ChildSpec struct {
	Name          string   `yaml:"name"`
	Enabled       *bool    `yaml:"enabled"`
	Cmd           string   `yaml:"cmd"`
	Args          []string `yaml:"args"`
	Cwd           string   `yaml:"cwd"`
	Env           []string `yaml:"env"`
	HealthTCPPort int      `yaml:"health_tcp_port"`
	StartupGraceS float64  `yaml:"startup_grace_s"`
}

// InstallCfg wird nur von den Verben install/uninstall/apply-rules gelesen (Windows).
type InstallCfg struct {
	ServiceName       string   `yaml:"service_name"`
	DisplayName       string   `yaml:"display_name"`
	Description       string   `yaml:"description"`
	Account           string   `yaml:"account"` // "" = LocalSystem, sonst z. B. NT SERVICE\SkirnirAgent
	GrantRead         []string `yaml:"grant_read"`
	GrantModify       []string `yaml:"grant_modify"`
	AllowControlUsers []string `yaml:"allow_control_users"`
	Firewall          []struct {
		Name    string   `yaml:"name"`
		Port    int      `yaml:"port"`
		Program string   `yaml:"program"`
		Remote  []string `yaml:"remote"` // erlaubte Quell-IPs (leer = alle)
	} `yaml:"firewall"`
}

// Umbenennung 2026-09-25 (0.11.0): ollama-router-agent -> skirnir-agent. Eine alte Installation bleibt lesbar, bis `setup`
// sie umgezogen hat - auch nach einem Selbst-Update unter dem alten Dateinamen.
const (
	legacyConfigPathWindows = `C:\ProgramData\ollama-router-agent\config.yaml`
	legacyConfigPathOther   = "/etc/ollama-router-agent/config.yaml"
	legacyServiceName       = "OllamaRouterAgent"
)

// defaultConfigPath: Windows ProgramData, sonst /etc; fehlt die neue Datei und die alte ist da, gilt die alte.
var defaultConfigPath = func() string {
	p, legacy := "/etc/skirnir-agent/config.yaml", legacyConfigPathOther
	if runtime.GOOS == "windows" {
		p, legacy = `C:\ProgramData\skirnir-agent\config.yaml`, legacyConfigPathWindows
	}
	if _, err := os.Stat(p); err != nil {
		if _, err := os.Stat(legacy); err == nil || os.IsPermission(err) { // "kein Zugriff" heisst: da (Steuer-Benutzer duerfen nicht lesen)
			return legacy
		}
	}
	return p
}()

func loadConfig(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := yaml.Unmarshal(raw, &c); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	c.path = path
	c.applyDefaults()
	if err := c.validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &c, nil
}

func (c *Config) applyDefaults() {
	if c.Node == "" {
		if h, err := os.Hostname(); err == nil {
			c.Node = strings.ToLower(h)
		}
	}
	// Basisordner fuer identity/logs/tls: der Ordner der geladenen Config, nicht der Standardpfad - sonst teilt ein
	// Testlauf mit --config X die Identitaet und Provisionierung des Dienstes (Review 2026-09-25)
	base := filepath.Dir(defaultConfigPath)
	if c.path != "" {
		if abs, err := filepath.Abs(c.path); err == nil {
			base = filepath.Dir(abs)
		}
	}
	if c.Identity.Dir == "" {
		c.Identity.Dir = filepath.Join(base, "identity")
	}
	if c.MQTT.Host != "" {
		mqttDefaults(&c.MQTT, c.Node)
	}
	if c.SecretStore.Environment == "" {
		c.SecretStore.Environment = "prod"
	}
	if c.SecretStore.SecretPath == "" {
		c.SecretStore.SecretPath = "/"
	}
	if c.Health.Listen == "" {
		c.Health.Listen = "127.0.0.1:10398"
	}
	if c.Logging.Dir == "" {
		c.Logging.Dir = filepath.Join(base, "logs")
	}
	if c.Logging.MaxSizeMB <= 0 {
		c.Logging.MaxSizeMB = 5
	}
	if c.Logging.MaxBackups <= 0 {
		c.Logging.MaxBackups = 10
	}
	for i := range c.Children {
		if c.Children[i].StartupGraceS <= 0 {
			c.Children[i].StartupGraceS = 90
		}
	}
	px := &c.OllamaProxy
	if px.Listen == "" {
		px.Listen = "0.0.0.0:11443"
	}
	if px.Upstream == "" {
		px.Upstream = "http://127.0.0.1:11434"
	}
	if px.CertDir == "" {
		px.CertDir = filepath.Join(base, "tls")
	}
	if c.Tunnel.Upstream == "" {
		c.Tunnel.Upstream = px.Upstream
	}
	for i := range c.Tunnel.Routes {
		r := &c.Tunnel.Routes[i]
		r.Upstream = strings.TrimRight(r.Upstream, "/")
		for j := range r.Methods {
			r.Methods[j] = strings.ToUpper(strings.TrimSpace(r.Methods[j]))
		}
	}
	in := &c.Install
	if in.ServiceName == "" {
		in.ServiceName = "SkirnirAgent"
	}
	if in.DisplayName == "" {
		in.DisplayName = "Skirnir Agent" // Anzeigename (services.msc); der technische Name SkirnirAgent bleibt
	}
	if in.Description == "" {
		in.Description = "Skirnir Agent: tunnel to the router with GPU heartbeat and GPU guard, MQTT device for Home Assistant, supervision of local AI services (Ollama, STT)."
	}
}

// mqttDefaults fuellt Luecken einer MQTT-Konfiguration (lokal oder vom Router provisioniert).
func mqttDefaults(m *MQTTCfg, node string) {
	if m.Port == 0 {
		m.Port = 1883
		if m.TLS {
			m.Port = 8883
		}
	}
	if m.DiscoveryPrefix == "" {
		m.DiscoveryPrefix = "homeassistant"
	}
	if m.IntervalS <= 0 {
		m.IntervalS = 30
	}
	if m.OllamaURL == "" {
		m.OllamaURL = "http://127.0.0.1:11434"
	}
	// AliasModel bleibt leer, wenn nicht gesetzt: keine Alias-Entitaeten. Lokal in mqtt.alias_model setzen (z. B. "assist:latest").
	if m.SttChild == "" {
		m.SttChild = "stt"
	}
	if m.SttFallbackPort == 0 {
		m.SttFallbackPort = 10300
	}
	if m.DeviceID == "" {
		m.DeviceID = strings.ReplaceAll(strings.ToLower(node), "-", "_")
	}
	if m.DeviceName == "" {
		m.DeviceName = node
	}
}

func (c *Config) validate() error {
	if c.Node == "" {
		return fmt.Errorf("node missing and host name not available")
	}
	if c.Router.URL == "" {
		return fmt.Errorf("router.url missing")
	}
	if c.MQTT.on() && c.MQTT.Host != "" { // lokale MQTT-Config nur pruefen, wenn gesetzt (sonst provisioniert der Router)
		if c.MQTT.Password == "" && c.MQTT.PasswordSecret == "" {
			return fmt.Errorf("mqtt.password or mqtt.password_secret missing")
		}
		if c.MQTT.PasswordSecret != "" && (c.SecretStore.ProjectID == "" || c.SecretStore.ClientID == "" || c.SecretStore.ClientSecret == "") {
			return fmt.Errorf("mqtt.password_secret set, but secret_store.project_id/client_id/client_secret missing")
		}
		if c.MQTT.PasswordSecret != "" && !strings.HasPrefix(c.SecretStore.Domain, "https://") {
			return fmt.Errorf("secret_store.domain missing or not an https:// URL (this used to be a silent placeholder)")
		}
	}
	if c.OllamaProxy.on() && c.OllamaProxy.requireToken() && c.Router.Token == "" {
		return fmt.Errorf("ollama_proxy.enabled needs router.token (or ollama_proxy.require_token: false) - otherwise Ollama would be open to the LAN")
	}
	seen := map[string]bool{}
	for _, ch := range c.Children {
		if ch.Name == "" || ch.Cmd == "" {
			return fmt.Errorf("children: name and cmd are required")
		}
		if seen[ch.Name] {
			return fmt.Errorf("children: duplicate name %q", ch.Name)
		}
		seen[ch.Name] = true
	}
	return validateRoutes(c.Tunnel.Routes)
}

var (
	routeNameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)
	httpMethods = map[string]bool{"GET": true, "HEAD": true, "POST": true, "PUT": true, "PATCH": true, "DELETE": true, "OPTIONS": true}
)

// validateRoutes: tunnel.routes darf nur auf diesen Rechner zeigen. Ein Upstream im Netz machte den Tunnel zu einem Weg vom
// Router in fremde Dienste; deshalb laedt die Config dann nicht, statt die Route still auszulassen.
func validateRoutes(routes []RouteCfg) error {
	names, paths := map[string]bool{}, map[string]bool{}
	for _, r := range routes {
		if !routeNameRe.MatchString(r.Name) {
			return fmt.Errorf("tunnel.routes: name %q missing or invalid (a-z, 0-9, _ and -)", r.Name)
		}
		if names[r.Name] {
			return fmt.Errorf("tunnel.routes: duplicate name %q", r.Name)
		}
		names[r.Name] = true
		if !strings.HasPrefix(r.Path, "/") || strings.ContainsAny(r.Path, "?#") {
			return fmt.Errorf("tunnel.routes[%s]: path %q must start with / and must not contain a query", r.Name, r.Path)
		}
		if paths[r.Path] {
			return fmt.Errorf("tunnel.routes[%s]: duplicate path %q", r.Name, r.Path)
		}
		paths[r.Path] = true
		if len(r.Methods) == 0 {
			return fmt.Errorf("tunnel.routes[%s]: methods missing", r.Name)
		}
		for _, m := range r.Methods {
			if !httpMethods[m] {
				return fmt.Errorf("tunnel.routes[%s]: unknown method %q", r.Name, m)
			}
		}
		if !loopbackUpstream(r.Upstream) {
			return fmt.Errorf("tunnel.routes[%s]: upstream %q is not a loopback URL (only http://127.0.0.1:<port> or http://[::1]:<port>)", r.Name, r.Upstream)
		}
		if !strings.HasPrefix(r.Health, "/") || strings.ContainsAny(r.Health, "#") {
			return fmt.Errorf("tunnel.routes[%s]: health %q must be a path (e.g. /health)", r.Name, r.Health)
		}
	}
	return nil
}

// loopbackUpstream: http(s) auf 127.0.0.1 oder ::1, ohne Pfad, Query und Zugangsdaten. "localhost" zaehlt nicht: der Name
// geht durch die Namensaufloesung und kann dort anders landen.
func loopbackUpstream(s string) bool {
	u, err := url.Parse(s)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return false
	}
	ip := net.ParseIP(u.Hostname())
	return ip != nil && (ip.Equal(net.IPv4(127, 0, 0, 1)) || ip.Equal(net.IPv6loopback))
}

func (m *MQTTCfg) on() bool   { return m.Enabled == nil || *m.Enabled }
func (s *ChildSpec) on() bool { return s.Enabled == nil || *s.Enabled }
