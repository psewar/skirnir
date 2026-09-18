package main

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Provision: das Konfigurationspaket, das der Router nach der Freigabe durch den Tunnel schickt (und bei Aenderungen
// nachschiebt). Der Agent braucht dadurch lokal nur noch die Router-URL; MQTT-Zugang und Intervalle kommen vom Router.
// Wird DPAPI-geschuetzt (Windows) unter <identity_dir>/provisioned.json abgelegt, damit der Agent auch startet, wenn
// der Router gerade nicht erreichbar ist.
type Provision struct {
	HeartbeatIntervalS float64  `json:"heartbeat_interval_s,omitempty"`
	MQTT               *MQTTCfg `json:"mqtt,omitempty"`
	Node               string   `json:"node,omitempty"` // Name, unter dem der Router den Knoten fuehrt
}

func provisionPath(dir string) string { return filepath.Join(dir, "provisioned.json") }

func loadProvision(dir string) (*Provision, error) {
	raw, err := os.ReadFile(provisionPath(dir))
	if err != nil {
		return nil, err
	}
	plain, err := unprotectBytes(raw)
	if err != nil {
		return nil, err
	}
	var p Provision
	if err := json.Unmarshal(plain, &p); err != nil {
		return nil, err
	}
	return &p, nil
}

func saveProvision(dir string, p *Provision) error {
	plain, err := json.Marshal(p)
	if err != nil {
		return err
	}
	blob, err := protectBytes(plain)
	if err != nil {
		return err
	}
	return os.WriteFile(provisionPath(dir), blob, 0o600)
}

func (p *Provision) equal(o *Provision) bool {
	a, _ := json.Marshal(p)
	b, _ := json.Marshal(o)
	return string(a) == string(b)
}
