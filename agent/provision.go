package main

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Provision: the configuration package the router sends through the tunnel after release (and pushes
// on changes). The agent thus only needs the router URL locally; MQTT access and intervals come from the router.
// Stored DPAPI-protected (Windows) at <identity_dir>/provisioned.json so the agent starts even if
// the router is currently unreachable.
type Provision struct {
	HeartbeatIntervalS float64  `json:"heartbeat_interval_s,omitempty"`
	MQTT               *MQTTCfg `json:"mqtt,omitempty"`
	Node               string   `json:"node,omitempty"` // Name under which the router manages the node
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
