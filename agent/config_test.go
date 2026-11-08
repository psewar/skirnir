package main

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func writeCfg(t *testing.T, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

// Review 2026-09-25: identity/logs were always next to the standard config, even with `--config X` - a test run thus shared the
// identity and provisioning of the service.
func TestConfigBaseDirFromConfigPath(t *testing.T) {
	p := writeCfg(t, "router:\n  url: https://router.example.net:11435\n")
	c, err := loadConfig(p)
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Dir(p)
	if c.Identity.Dir != filepath.Join(dir, "identity") || c.Logging.Dir != filepath.Join(dir, "logs") || c.OllamaProxy.CertDir != filepath.Join(dir, "tls") {
		t.Fatalf("Basisordner: identity=%s logs=%s tls=%s (erwartet unter %s)", c.Identity.Dir, c.Logging.Dir, c.OllamaProxy.CertDir, dir)
	}
	if c.SecretStore.Domain != "" {
		t.Fatalf("secret_store.domain hat still einen Platzhalter: %q", c.SecretStore.Domain)
	}
}

func TestConfigSecretStoreDomainRequired(t *testing.T) {
	body := "router:\n  url: https://r.example.net\nmqtt:\n  host: mqtt.example.net\n  password_secret: MQTT_PASSWORD\n" +
		"secret_store:\n  project_id: p\n  client_id: c\n  client_secret: s\n"
	if _, err := loadConfig(writeCfg(t, body)); err == nil || !strings.Contains(err.Error(), "domain") {
		t.Fatalf("ohne domain akzeptiert (Client-Secret ginge an einen Platzhalter-Host): %v", err)
	}
	if _, err := loadConfig(writeCfg(t, body+"  domain: https://secrets.example.net\n")); err != nil {
		t.Fatalf("mit domain abgelehnt: %v", err)
	}
}

// ADR-0051: a top-level key no Config field reads (a misspelt block, or one retired in an earlier version) stops the
// load with its name instead of leaving its settings silently unused.
func TestConfigUnknownTopLevelKeyRejected(t *testing.T) {
	base := "router:\n  url: https://r.example.net\n"
	retired := base + "old_secrets:\n  domain: https://secrets.example.net\n  project_id: p\n  client_id: c\n  client_secret: s\n"
	_, err := loadConfig(writeCfg(t, retired))
	if err == nil || !strings.Contains(err.Error(), `unknown top-level key(s) "old_secrets"`) || !strings.Contains(err.Error(), "secret_store") {
		t.Fatalf("unknown block accepted or message without its name and the known keys: %v", err)
	}
	if _, err := loadConfig(writeCfg(t, base+"Router:\n  url: https://r.example.net\n")); err == nil || !strings.Contains(err.Error(), `"Router"`) {
		t.Fatalf("key with wrong case accepted (yaml.v3 would ignore it): %v", err)
	}
	// a key that only holds a YAML anchor is not a Config block either
	anchor := base + "defaults: &d\n  listen: 127.0.0.1:10398\n"
	if _, err := loadConfig(writeCfg(t, anchor)); err == nil {
		t.Fatalf("anchor holder 'defaults' is not a Config block and must be rejected")
	}
	// accepted: known blocks, a YAML merge key, and a retired nested key (only top-level keys are checked)
	merged := "<<: {node: n1}\n" + base + "tunnel:\n  enabled: true\nhealth:\n  listen: 127.0.0.1:10398\n"
	c, err := loadConfig(writeCfg(t, merged))
	if err != nil {
		t.Fatalf("merge key or retired nested key rejected: %v", err)
	}
	if c.Node != "n1" {
		t.Fatalf("merge key not applied: node=%q", c.Node)
	}
}

// Every top-level block that config.example.yaml documents (commented or not) is one loadConfig accepts.
func TestConfigExampleTopLevelKeysKnown(t *testing.T) {
	raw, err := os.ReadFile("config.example.yaml")
	if err != nil {
		t.Fatal(err)
	}
	known := knownTopLevelKeys()
	re := regexp.MustCompile(`^(?:# ?)?([a-z_]+):(?:\s|$)`)
	seen := 0
	for _, l := range strings.Split(strings.ReplaceAll(string(raw), "\r", ""), "\n") {
		if m := re.FindStringSubmatch(l); m != nil {
			seen++
			if !known[m[1]] {
				t.Errorf("config.example.yaml documents %q, which loadConfig rejects as unknown", m[1])
			}
		}
	}
	if seen < 10 {
		t.Fatalf("only %d top-level keys found in config.example.yaml; the pattern no longer matches its layout", seen)
	}
}

func TestConfigProxyNeedsToken(t *testing.T) {
	if _, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\nollama_proxy:\n  enabled: true\n")); err == nil || !strings.Contains(err.Error(), "router.token") {
		t.Fatalf("Proxy ohne Token akzeptiert (waere offen fuer das LAN): %v", err)
	}
	if _, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\n  token: abc\nollama_proxy:\n  enabled: true\n")); err != nil {
		t.Fatalf("Proxy mit Token abgelehnt: %v", err)
	}
	if _, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\nollama_proxy:\n  enabled: true\n  require_token: false\n")); err != nil {
		t.Fatalf("Proxy mit require_token: false abgelehnt: %v", err)
	}
}
