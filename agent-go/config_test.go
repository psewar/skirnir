package main

import (
	"os"
	"path/filepath"
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

// Review 2026-09-25: identity/logs lagen immer neben der Standard-Config, auch bei `--config X` - ein Testlauf teilte so die
// Identitaet und Provisionierung des Dienstes.
func TestConfigBasisordnerAusConfigPfad(t *testing.T) {
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

func TestConfigSecretStoreDomainPflicht(t *testing.T) {
	body := "router:\n  url: https://r.example.net\nmqtt:\n  host: mqtt.example.net\n  password_secret: MQTT_PASSWORD\n" +
		"secret_store:\n  project_id: p\n  client_id: c\n  client_secret: s\n"
	if _, err := loadConfig(writeCfg(t, body)); err == nil || !strings.Contains(err.Error(), "domain") {
		t.Fatalf("ohne domain akzeptiert (Client-Secret ginge an einen Platzhalter-Host): %v", err)
	}
	if _, err := loadConfig(writeCfg(t, body+"  domain: https://secrets.example.net\n")); err != nil {
		t.Fatalf("mit domain abgelehnt: %v", err)
	}
	// alter Blockname `mimir:` bleibt lesbar
	legacy := "router:\n  url: https://r.example.net\nmqtt:\n  host: mqtt.example.net\n  password_secret: MQTT_PASSWORD\n" +
		"mimir:\n  domain: https://secrets.example.net\n  project_id: p\n  client_id: c\n  client_secret: s\n"
	if c, err := loadConfig(writeCfg(t, legacy)); err != nil || c.SecretStore.ProjectID != "p" {
		t.Fatalf("Legacy-Block mimir: %v", err)
	}
}

func TestConfigProxyBrauchtToken(t *testing.T) {
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
