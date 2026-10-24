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

// Config is the agent's local configuration file (YAML). Since the tunnel with key identity
// a node only needs router.url; the agent reports Name, GPU, MAC itself, and the Router provisions MQTT access and intervals
// after release. Local settings here override the provisioned ones.
type Config struct {
	Node         string `yaml:"node"` // leer = Hostname (klein)
	Router       RouterCfg
	MQTT         MQTTCfg                 // optional: local override (Host set) or enabled: false
	SecretStore  SecretStoreCfg          `yaml:"secret_store"` // optional: only for mqtt.password_secret (Universal-Auth-API)
	Health       struct{ Listen string } `yaml:"health"`
	Logging      LogCfg
	Children     []ChildSpec `yaml:"children"`
	Install      InstallCfg
	OllamaProxy  ProxyCfg        `yaml:"ollama_proxy"`
	Tunnel       TunnelCfg       `yaml:"tunnel"`
	GPUZ         GPUZCfg         `yaml:"gpuz"`          // GPU-Z-Sensoren als Zusatzquelle (Windows), Standard an
	GPUGuard     GuardCfg        `yaml:"gpu_guard"`     // GPU guard: Power limit, high-load stage, warnings (guard.go), enabled by default
	Update       UpdateCfg       `yaml:"update"`        // Self-update via the Router (updater.go): operator's public_key, enabled
	OllamaUpdate OllamaUpdateCfg `yaml:"ollama_update"` // Ollama update via the Router (ollamaupdate.go, 0.12.0): enabled, source
	path         string          // Path of the loaded file (for restart after an update)
	Identity     struct {
		Dir string `yaml:"dir"`
	} `yaml:"identity"`
}

// TunnelCfg: outgoing persistent connection to the Router, through which the Router calls Ollama. Since 0.9.1 the only way
// (the HTTP-Heartbeat with token and `tunnel.enabled` are gone).
type TunnelCfg struct {
	Upstream string `yaml:"upstream"`
	// UpstreamAPI: which API the upstream speaks. "ollama" (default) or "openai" for an OpenAI-compatible inference server
	// (/v1/models, /v1/chat/completions) such as Strata. With "openai" the agent reports the server's models from
	// /v1/models in the OLLAMA frame and tells the router in its facts, so the router sends chat requests in OpenAI form.
	UpstreamAPI string `yaml:"upstream_api"`
	// UpstreamChild: the child (children[].name) that runs an OpenAI-compatible upstream. With it the router may stop the
	// server to free the GPU while the node is busy (a game) and start it again when the node is free (upstream_hold.go).
	// An OpenAI server cannot unload its model the way Ollama does, so without this a busy node keeps the card full.
	UpstreamChild string `yaml:"upstream_child"`
	// UpstreamFallback (agent 0.23.0): an Ollama child that serves while the upstream is held for a game. Without it the
	// node has nothing to offer during a game; with it the small busy_ok models (voice assistant) keep running.
	UpstreamFallback *FallbackCfg `yaml:"upstream_fallback"`
	Routes           []RouteCfg   `yaml:"routes"` // optional: additional local services by path (routes.go); empty = all to upstream
}

// FallbackCfg: tunnel.upstream_fallback (upstream_hold.go). The child is stopped while the OpenAI-compatible upstream
// runs and started while it is held, so the two never share the card.
type FallbackCfg struct {
	Child    string `yaml:"child"`    // children[].name of an Ollama child
	Upstream string `yaml:"upstream"` // loopback URL of that Ollama, default http://127.0.0.1:11434
	// MaxRAMPercent (agent 0.26.0): the agent starts the held upstream only if the RAM in use plus the upstream's measured
	// need stays at or below this share of the RAM; otherwise it stays held and reports upstream_blocked. Default 90,
	// 0 = no check. Trigger 2026-10-24: Strata started at 98 % RAM, a bluescreen followed seconds later.
	MaxRAMPercent *int `yaml:"max_ram_percent"`
}

const defaultMaxRAMPercent = 90

// Values of tunnel.upstream_api.
const (
	upstreamOllama = "ollama"
	upstreamOpenAI = "openai"
)

// RouteCfg: a path that the tunnel routes to another local service instead of Ollama (e.g. the Reranker).
type RouteCfg struct {
	Name     string   `yaml:"name"`     // Schluessel im Heartbeat-Feld routes
	Path     string   `yaml:"path"`     // exact match with the request path (without query)
	Methods  []string `yaml:"methods"`  // erlaubte Methoden; andere bekommen 405
	Upstream string   `yaml:"upstream"` // loopback only: http://127.0.0.1:<port> or http://[::1]:<port>
	Health   string   `yaml:"health"`   // Path of the health check on the upstream, every 5 s
}

// GPUZCfg: GPU-Z-Shared-Memory als Zusatzquelle (Speichertemperatur, Hot Spot, Spannung, 16-Pin-Leistung). Standard an;
// if GPU-Z is not running, nothing happens. `enabled: false` disables the reader entirely.
type GPUZCfg struct {
	Enabled *bool `yaml:"enabled"`
}

func (g *GPUZCfg) on() bool { return g.Enabled == nil || *g.Enabled }

// ProxyCfg: TLS front-end before local Ollama (Opt-in, alternative without tunnel).
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
	Token string `yaml:"token"` // only for the TLS front-end (ollama_proxy: X-Router-Token)
}

// MQTTCfg is read both locally (YAML) and provisioned by the Router (JSON).
type MQTTCfg struct {
	Enabled         *bool   `yaml:"enabled" json:"enabled,omitempty"`
	Host            string  `yaml:"host" json:"host"`
	Port            int     `yaml:"port" json:"port"`
	TLS             bool    `yaml:"tls" json:"tls"`
	Username        string  `yaml:"username" json:"username"`
	Password        string  `yaml:"password" json:"password"`
	PasswordSecret  string  `yaml:"password_secret" json:"-"` // Key in Secret-Store (local only)
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

// SecretStoreCfg: access to a Secret-Store with Universal-Auth-API (Client-ID/-Secret -> Token -> Secret per key).
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

// ChildSpec describes a monitored child process (e.g. the Wyoming-STT-Server).
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

// InstallCfg is only read by the install/uninstall/apply-rules verbs (Windows).
type InstallCfg struct {
	ServiceName       string   `yaml:"service_name"`
	DisplayName       string   `yaml:"display_name"`
	Description       string   `yaml:"description"`
	Account           string   `yaml:"account"` // "" = LocalSystem, otherwise e.g. NT SERVICE\SkirnirAgent
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

// Rename 2026-09-25 (0.11.0): ollama-router-agent -> skirnir-agent. An old installation remains readable until `setup`
// has moved it - even after a self-update under the old filename.
const (
	legacyConfigPathWindows = `C:\ProgramData\ollama-router-agent\config.yaml`
	legacyConfigPathOther   = "/etc/ollama-router-agent/config.yaml"
	legacyServiceName       = "OllamaRouterAgent"
)

// defaultConfigPath: Windows ProgramData, otherwise /etc; if the new file is missing and the old one exists, the old one applies.
var defaultConfigPath = func() string {
	p, legacy := "/etc/skirnir-agent/config.yaml", legacyConfigPathOther
	if runtime.GOOS == "windows" {
		p, legacy = `C:\ProgramData\skirnir-agent\config.yaml`, legacyConfigPathWindows
	}
	if _, err := os.Stat(p); err != nil {
		if _, err := os.Stat(legacy); err == nil || os.IsPermission(err) { // "no access" means: present (Control users must not read)
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
	// Base folder for identity/logs/tls: the folder of the loaded Config, not the default path - otherwise a
	// test run with --config X shares the identity and provisioning of the service (Review 2026-09-25)
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
	if c.Tunnel.UpstreamAPI == "" {
		c.Tunnel.UpstreamAPI = upstreamOllama
	}
	if fb := c.Tunnel.UpstreamFallback; fb != nil {
		if fb.Upstream == "" {
			fb.Upstream = "http://127.0.0.1:11434"
		}
		fb.Upstream = strings.TrimRight(fb.Upstream, "/")
		if fb.MaxRAMPercent == nil {
			p := defaultMaxRAMPercent
			fb.MaxRAMPercent = &p
		}
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
		in.DisplayName = "Skirnir Agent" // Display name (services.msc); the technical name SkirnirAgent remains
	}
	if in.Description == "" {
		in.Description = "Skirnir Agent: tunnel to the router with GPU heartbeat and GPU guard, MQTT device for Home Assistant, supervision of local AI services (Ollama, STT)."
	}
}

// mqttDefaults fills gaps in an MQTT configuration (local or provisioned by the Router).
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
	// AliasModel remains empty if not set: no alias entities. Set locally in mqtt.alias_model (e.g. "assist:latest").
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
	if c.MQTT.on() && c.MQTT.Host != "" { // only check local MQTT-Config if set (otherwise the Router provisions it)
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
	if c.Tunnel.UpstreamAPI != upstreamOllama && c.Tunnel.UpstreamAPI != upstreamOpenAI {
		return fmt.Errorf("tunnel.upstream_api: %q is not ollama or openai", c.Tunnel.UpstreamAPI)
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
	if uc := c.Tunnel.UpstreamChild; uc != "" {
		if c.Tunnel.UpstreamAPI != upstreamOpenAI {
			return fmt.Errorf("tunnel.upstream_child needs tunnel.upstream_api: openai (Ollama unloads models itself)")
		}
		if !seen[uc] {
			return fmt.Errorf("tunnel.upstream_child: %q is not a child (children[].name)", uc)
		}
	}
	if err := c.validateFallback(); err != nil {
		return err
	}
	return validateRoutes(c.Tunnel.Routes)
}

// validateFallback: tunnel.upstream_fallback needs a held upstream to stand in for, an enabled Ollama child of its own
// and a loopback URL.
func (c *Config) validateFallback() error {
	fb := c.Tunnel.UpstreamFallback
	if fb == nil {
		return nil
	}
	if c.Tunnel.UpstreamChild == "" {
		return fmt.Errorf("tunnel.upstream_fallback needs tunnel.upstream_child (it serves while that child is held)")
	}
	if fb.Child == c.Tunnel.UpstreamChild {
		return fmt.Errorf("tunnel.upstream_fallback.child: %q is the upstream child itself", fb.Child)
	}
	var spec *ChildSpec
	for i := range c.Children {
		if c.Children[i].Name == fb.Child {
			spec = &c.Children[i]
		}
	}
	if spec == nil {
		return fmt.Errorf("tunnel.upstream_fallback.child: %q is not a child (children[].name)", fb.Child)
	}
	if !spec.on() {
		return fmt.Errorf("tunnel.upstream_fallback.child: %q is disabled (enabled: false); the agent starts it only while the upstream is held", fb.Child)
	}
	if !isOllamaExe(spec.Cmd) {
		return fmt.Errorf("tunnel.upstream_fallback.child: %q does not run ollama (cmd %q)", fb.Child, spec.Cmd)
	}
	if !loopbackUpstream(fb.Upstream) {
		return fmt.Errorf("tunnel.upstream_fallback.upstream: %q is not a loopback URL (only http://127.0.0.1:<port> or http://[::1]:<port>)", fb.Upstream)
	}
	if p := *fb.MaxRAMPercent; p != 0 && (p < 50 || p > 99) {
		return fmt.Errorf("tunnel.upstream_fallback.max_ram_percent: %d - 50 to 99, or 0 for no check", p)
	}
	return nil
}

var (
	routeNameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)
	httpMethods = map[string]bool{"GET": true, "HEAD": true, "POST": true, "PUT": true, "PATCH": true, "DELETE": true, "OPTIONS": true}
)

// validateRoutes: tunnel.routes may only point to this machine. An upstream on the network would make the tunnel a path from the
// Router to foreign services; therefore the Config fails to load, instead of silently omitting the route.
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

// loopbackUpstream: http(s) on 127.0.0.1 or ::1, without path, query and credentials. "localhost" does not count: the name
// goes through name resolution and may land elsewhere.
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
