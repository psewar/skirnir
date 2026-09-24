package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"gopkg.in/yaml.v3"
)

// Config ist die eine lokale Konfigurationsdatei des Agenten (YAML). Seit dem Tunnel mit Schluessel-Identitaet
// braucht ein Knoten nur noch router.url; Name, GPU, MAC meldet der Agent selbst, MQTT-Zugang und Intervalle
// provisioniert der Router nach der Freigabe. Lokale Angaben hier ueberschreiben das Provisionierte.
type Config struct {
	Node        string `yaml:"node"` // leer = Hostname (klein)
	Router      RouterCfg
	MQTT        MQTTCfg                 // optional: lokale Ueberschreibung (Host gesetzt) oder enabled: false
	SecretStore SecretStoreCfg          `yaml:"secret_store"` // optional: nur fuer mqtt.password_secret (Universal-Auth-API)
	MimirLegacy SecretStoreCfg          `yaml:"mimir"`        // alter Name des Blocks; wird in applyDefaults uebernommen
	Health      struct{ Listen string } `yaml:"health"`
	Logging     LogCfg
	Children    []ChildSpec `yaml:"children"`
	Install     InstallCfg
	OllamaProxy ProxyCfg  `yaml:"ollama_proxy"`
	Tunnel      TunnelCfg `yaml:"tunnel"`
	GPUZ        GPUZCfg   `yaml:"gpuz"`      // GPU-Z-Sensoren als Zusatzquelle (Windows), Standard an
	GPUGuard    GuardCfg  `yaml:"gpu_guard"` // GPU-Schutz: Power-Limit, Hochlast-Stufe, Warnungen (guard.go), Standard an
	Identity    struct {
		Dir string `yaml:"dir"`
	} `yaml:"identity"`
}

// TunnelCfg: ausgehende Dauerverbindung zum Router, durch die der Router Ollama aufruft (Standard an).
type TunnelCfg struct {
	Enabled  *bool  `yaml:"enabled"`
	Upstream string `yaml:"upstream"`
}

func (t *TunnelCfg) on() bool { return t.Enabled == nil || *t.Enabled }

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
	URL       string  `yaml:"url"`
	Token     string  `yaml:"token"` // nur noch fuer den alten HTTP-Heartbeat / TLS-Proxy
	IntervalS float64 `yaml:"interval_s"`
	TimeoutS  float64 `yaml:"timeout_s"`
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
	Account           string   `yaml:"account"` // "" = LocalSystem, sonst z. B. NT SERVICE\OllamaRouterAgent
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

// defaultConfigPath: Windows ProgramData, sonst /etc.
var defaultConfigPath = func() string {
	if runtime.GOOS == "windows" {
		return `C:\ProgramData\ollama-router-agent\config.yaml`
	}
	return "/etc/ollama-router-agent/config.yaml"
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
	base := filepath.Dir(defaultConfigPath)
	if c.Identity.Dir == "" {
		c.Identity.Dir = filepath.Join(base, "identity")
	}
	if c.Router.IntervalS <= 0 {
		c.Router.IntervalS = 3
	}
	if c.Router.TimeoutS <= 0 {
		c.Router.TimeoutS = 5
	}
	if c.MQTT.Host != "" {
		mqttDefaults(&c.MQTT, c.Node)
	}
	if c.SecretStore == (SecretStoreCfg{}) { // alter Blockname `mimir:` weiter lesbar
		c.SecretStore = c.MimirLegacy
	}
	if c.SecretStore.Domain == "" {
		c.SecretStore.Domain = "https://secrets.example.net"
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
	in := &c.Install
	if in.ServiceName == "" {
		in.ServiceName = "OllamaRouterAgent"
	}
	if in.DisplayName == "" {
		in.DisplayName = "Ollama Router Agent"
	}
	if in.Description == "" {
		in.Description = "Tunnel zum Ollama-Router mit GPU-Heartbeat, MQTT-Geraet fuer Home Assistant, Aufsicht ueber lokale KI-Dienste (STT)."
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
		return fmt.Errorf("node fehlt und Hostname nicht ermittelbar")
	}
	if c.Router.URL == "" {
		return fmt.Errorf("router.url fehlt")
	}
	if !c.Tunnel.on() && c.Router.Token == "" {
		return fmt.Errorf("ohne Tunnel braucht der HTTP-Heartbeat router.token")
	}
	if c.MQTT.on() && c.MQTT.Host != "" { // lokale MQTT-Config nur pruefen, wenn gesetzt (sonst provisioniert der Router)
		if c.MQTT.Password == "" && c.MQTT.PasswordSecret == "" {
			return fmt.Errorf("mqtt.password oder mqtt.password_secret fehlt")
		}
		if c.MQTT.PasswordSecret != "" && (c.SecretStore.ProjectID == "" || c.SecretStore.ClientID == "" || c.SecretStore.ClientSecret == "") {
			return fmt.Errorf("mqtt.password_secret gesetzt, aber secret_store.project_id/client_id/client_secret fehlen")
		}
	}
	seen := map[string]bool{}
	for _, ch := range c.Children {
		if ch.Name == "" || ch.Cmd == "" {
			return fmt.Errorf("children: name und cmd sind Pflicht")
		}
		if seen[ch.Name] {
			return fmt.Errorf("children: Name %q doppelt", ch.Name)
		}
		seen[ch.Name] = true
	}
	return nil
}

func (m *MQTTCfg) on() bool   { return m.Enabled == nil || *m.Enabled }
func (s *ChildSpec) on() bool { return s.Enabled == nil || *s.Enabled }
