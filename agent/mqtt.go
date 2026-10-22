package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"crypto/tls"
	paho "github.com/eclipse/paho.mqtt.golang"
)

// MQTTModule replaces the former Python script: same device, same unique_ids, same topics,
// so HA entities and the cloud fallback automation keep working unchanged.
type MQTTModule struct {
	cfg           MQTTCfg
	secretStore   SecretStoreCfg
	legacyCleared bool // Discovery configs for previous object IDs cleared once per run
	node          string
	log           *Logger
	gpu           *GPU
	hb            *Heartbeat
	sup           *Supervisor
	guard         *Guard // GPU-Schutz (guard.go), nil in Tests/Verben
	http          *http.Client

	mu        sync.Mutex
	client    paho.Client
	connected bool
	lastPub   time.Time
	lastErr   string
	published int
	applies   map[string]bool // optional entity group -> applies on this node (missing = still unknown)
	announced map[string]bool // objID -> Discovery config is in the broker
}

// Families whose models cannot handle HA's ai_task structured output (evidence 2026-08-23).
var familiesWithoutStructure = map[string]bool{"gptoss": true}

type sensorDef struct {
	comp, objID, name, field, icon string
	extra                          map[string]any
}

// entityNames: English name of the entity ID where it differs from objID (since 0.20.0, design/english-names.md). objID
// determines unique_id and discovery topic and remains fixed for existing entities - a new unique_id would orphan history
// and automations.
var entityNames = map[string]string{
	"ollama_kuerzungen":      "ollama_truncations",
	"ollama_letzte_kuerzung": "ollama_last_truncation",
	"ollama_kuerzung":        "ollama_truncation",
}

func (s sensorDef) entityName() string {
	if n, ok := entityNames[s.objID]; ok {
		return n
	}
	return s.objID
}

// 1:1 from the former Python script.
var sensorDefs = []sensorDef{
	{"binary_sensor", "stt", "STT-Dienst", "stt", "mdi:microphone-message", nil},
	{"binary_sensor", "ollama", "Ollama", "ollama", "mdi:robot", nil},
	{"binary_sensor", "gaming_mode", "Gaming-Modus", "gaming_mode", "mdi:controller", nil},
	{"binary_sensor", "alias_model_structure", "Alias-Modell strukturfähig", "alias_model_structure", "mdi:code-json", nil},
	{"sensor", "alias_model", "Alias-Modell", "alias_model", "mdi:tag-text", nil},
	{"sensor", "vram_used_mb", "VRAM belegt", "vram_used_mb", "mdi:memory", map[string]any{"unit_of_measurement": "MB", "state_class": "measurement"}},
	{"sensor", "ollama_loaded", "Ollama geladene Modelle", "ollama_loaded", "mdi:layers", map[string]any{"state_class": "measurement"}},
	{"sensor", "ollama_loaded_names", "Ollama geladen", "ollama_loaded_names", "mdi:format-list-bulleted", nil},
	// new: view of the service itself
	{"sensor", "agent_version", "Agent Version", "agent_version", "mdi:package-variant", map[string]any{"entity_category": "diagnostic"}},
	{"binary_sensor", "router_heartbeat", "Router-Heartbeat", "router_heartbeat", "mdi:heart-pulse", map[string]any{"entity_category": "diagnostic"}},
	// Silent truncations in the Ollama log (truncation.go): counter since service start, timestamp + details of the last,
	// and an event per truncation for automations (topic <device>/kuerzung, not retained).
	{"sensor", "ollama_kuerzungen", "Ollama Kürzungen", "ollama_truncations", "mdi:content-cut", map[string]any{"state_class": "total_increasing"}},
	{"sensor", "ollama_letzte_kuerzung", "Ollama letzte Kürzung", "ollama_last_truncation", "mdi:clock-alert-outline",
		map[string]any{"device_class": "timestamp", "json_attributes_template": "{{ value_json.truncation_details | tojson }}"}},
	{"event", "ollama_kuerzung", "Ollama Kürzung", "", "mdi:content-cut",
		map[string]any{"event_types": []string{truncationInput, truncationContextFull}}},
	// Sensors (0.6.0). Group "gpu": NVML or nvidia-smi; group "gpuz": only where GPU-Z was seen once.
	{"sensor", "gpu_temp_c", "GPU Temperatur", "gpu_temp_c", "mdi:thermometer", tempExtra},
	{"sensor", "gpu_power_w", "GPU Leistung", "gpu_power_w", "mdi:flash", powerExtra},
	{"sensor", "gpu_power_limit_w", "GPU Power-Limit", "gpu_power_limit_w", "mdi:flash-alert", map[string]any{"unit_of_measurement": "W", "device_class": "power", "entity_category": "diagnostic"}},
	{"sensor", "gpu_fan_pct", "GPU Lüfter", "gpu_fan_pct", "mdi:fan", map[string]any{"unit_of_measurement": "%", "state_class": "measurement"}},
	{"sensor", "gpu_throttle", "GPU Drosselung", "gpu_throttle", "mdi:speedometer-slow", map[string]any{"entity_category": "diagnostic"}},
	{"sensor", "gpu_mem_temp_c", "GPU Speicher-Temperatur", "gpu_mem_temp_c", "mdi:thermometer-lines", tempExtra},
	{"sensor", "gpu_hotspot_c", "GPU Hot Spot", "gpu_hotspot_c", "mdi:thermometer-high", tempExtra},
	{"sensor", "gpu_voltage_v", "GPU Spannung", "gpu_voltage_v", "mdi:sine-wave", map[string]any{"unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement", "suggested_display_precision": 3}},
	{"sensor", "gpu_pin16_power_w", "GPU 16-Pin Leistung", "gpu_pin16_power_w", "mdi:power-plug", powerExtra},
	{"sensor", "gpu_pin16_voltage_v", "GPU 16-Pin Spannung", "gpu_pin16_voltage_v", "mdi:power-plug-outline", map[string]any{"unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement", "suggested_display_precision": 2}},
	{"sensor", "gpu_perfcap", "GPU PerfCap", "gpu_perfcap", "mdi:car-brake-alert", map[string]any{"entity_category": "diagnostic"}},
	{"sensor", "cpu_temp_c", "CPU Temperatur", "cpu_temp_c", "mdi:thermometer", tempExtra},
	// GPU guard (0.7.0, guard.go): state with attributes, problem sensor, events on <device>/gpu_guard
	{"sensor", "gpu_guard_state", "GPU-Schutz", "gpu_guard_state", "mdi:shield-half-full",
		map[string]any{"json_attributes_template": "{{ value_json.gpu_guard_details | tojson }}"}},
	{"binary_sensor", "gpu_guard_problem", "GPU-Schutz Problem", "gpu_guard_problem", "mdi:shield-alert", map[string]any{"device_class": "problem"}},
	{"sensor", "gpu_guard_target_w", "GPU-Schutz Ziel-Limit", "gpu_guard_target_w", "mdi:shield-check", map[string]any{"unit_of_measurement": "W", "device_class": "power", "entity_category": "diagnostic"}},
	{"event", "gpu_guard", "GPU-Schutz Ereignis", "", "mdi:shield-alert",
		map[string]any{"event_types": []string{guardEvHighLoad, guardEvThrottled, guardEvRecovery, guardEvLimitSet, guardEvLimitRefused, guardEvExternalChange, guardEvVoltageLow, guardEvTemperatureHigh, guardEvHwThrottle, guardEvDeselected}}},
}

var (
	tempExtra  = map[string]any{"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}
	powerExtra = map[string]any{"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"}
)

// Entities not present on every node: objID -> group. A group is only created if it means something on
// this machine - otherwise sensors hang in HA on foreign nodes, staying OFF forever.
//
//	stt   = a child process named mqtt.stt_child is configured
//	alias = the model mqtt.alias_model is in local Ollama (empty = group off)
var sensorGroup = map[string]string{
	"stt":                   "stt",
	"alias_model_structure": "alias",
	"alias_model":           "alias",
	"gpu_temp_c":            "gpu",
	"gpu_power_w":           "gpu",
	"gpu_power_limit_w":     "gpu",
	"gpu_fan_pct":           "gpu",
	"gpu_throttle":          "gpu",
	"gpu_mem_temp_c":        "gpuz",
	"gpu_hotspot_c":         "gpuz",
	"gpu_voltage_v":         "gpuz",
	"gpu_pin16_power_w":     "gpuz",
	"gpu_pin16_voltage_v":   "gpuz",
	"gpu_perfcap":           "gpuz",
	"cpu_temp_c":            "gpuz",
}

func newMQTT(cfg MQTTCfg, secretStore SecretStoreCfg, node string, log *Logger, gpu *GPU, hb *Heartbeat, sup *Supervisor, guard *Guard) *MQTTModule {
	return &MQTTModule{cfg: cfg, secretStore: secretStore, node: node, log: log, gpu: gpu, hb: hb, sup: sup, guard: guard,
		http:      &http.Client{Timeout: 8 * time.Second},
		applies:   map[string]bool{},
		announced: map[string]bool{}}
}

// setApplies notes if an optional group applies on this node. Only call with reliable info:
// "Ollama was not reachable" must not delete an entity.
func (m *MQTTModule) setApplies(group string, yes bool) {
	m.mu.Lock()
	m.applies[group] = yes
	m.mu.Unlock()
}

// wanted says if an entity should exist on this node. While a group is unknown, nothing is
// created and nothing is deleted (second return value false).
func (m *MQTTModule) wanted(objID string) (bool, bool) {
	g, ok := sensorGroup[objID]
	if !ok {
		return true, true
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	yes, known := m.applies[g]
	return yes, known
}

func (m *MQTTModule) broker() string {
	if m.cfg.TLS {
		return fmt.Sprintf("ssl://%s:%d", m.cfg.Host, m.cfg.Port)
	}
	return fmt.Sprintf("tcp://%s:%d", m.cfg.Host, m.cfg.Port)
}

func (m *MQTTModule) availTopic() string { return m.cfg.DeviceID + "/status" }
func (m *MQTTModule) stateTopic() string { return m.cfg.DeviceID + "/state" }
func (m *MQTTModule) eventTopic() string { return m.cfg.DeviceID + "/truncation" }
func (m *MQTTModule) guardTopic() string { return m.cfg.DeviceID + "/gpu_guard" }

func (m *MQTTModule) password(ctx context.Context) (string, error) {
	if m.cfg.Password != "" {
		return m.cfg.Password, nil
	}
	return fetchSecret(ctx, m.secretStore, m.cfg.PasswordSecret)
}

func (m *MQTTModule) Run(ctx context.Context) {
	// Fetch password, patiently retry on secret store failure (the service must not die because of it).
	var pw string
	for delay := 5 * time.Second; ; delay = min(delay*2, 5*time.Minute) {
		var err error
		if pw, err = m.password(ctx); err == nil {
			break
		}
		m.setErr("password: " + err.Error())
		m.log.Warnf("mqtt: %v, retrying in %s", err, delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
	}
	opts := paho.NewClientOptions().
		AddBroker(m.broker()).SetTLSConfig(&tls.Config{MinVersion: tls.VersionTLS12}).
		SetClientID("skirnir-agent-"+m.cfg.DeviceID).
		SetUsername(m.cfg.Username).SetPassword(pw).
		SetAutoReconnect(true).SetConnectRetry(true).SetConnectRetryInterval(10*time.Second).
		SetKeepAlive(30*time.Second).SetConnectTimeout(10*time.Second).
		SetWill(m.availTopic(), "offline", 1, true).
		SetOnConnectHandler(func(c paho.Client) {
			m.mu.Lock()
			m.connected = true
			m.announced = map[string]bool{} // reconcile retained configs after a reconnect
			m.mu.Unlock()
			m.log.Infof("mqtt: connected to %s", m.broker())
			st := m.collect(ctx) // first check what exists here, then create the matching entities
			m.syncDiscovery(c)
			c.Publish(m.availTopic(), 1, true, "online")
			m.publishStateOf(c, st)
		}).
		SetConnectionLostHandler(func(c paho.Client, err error) {
			m.mu.Lock()
			m.connected = false
			m.mu.Unlock()
			m.setErr(err.Error())
			m.log.Warnf("mqtt: connection lost: %v", err)
		})
	c := paho.NewClient(opts)
	m.mu.Lock()
	m.client = c
	m.mu.Unlock()
	if t := c.Connect(); t.WaitTimeout(15*time.Second) && t.Error() != nil {
		m.log.Warnf("mqtt: first connect: %v (retrying)", t.Error())
	}
	tick := time.NewTicker(time.Duration(m.cfg.IntervalS * float64(time.Second)))
	defer tick.Stop()
	var truncation <-chan Truncation
	if m.sup != nil {
		truncation = m.sup.truncations.fresh
	}
	var guardEv <-chan guardEvent
	if m.guard != nil {
		guardEv = m.guard.events
	}
	for {
		select {
		case e := <-guardEv: // report GPU guard event immediately, state along with it
			if c.IsConnected() {
				m.publishGuardEvent(c, e)
				m.publishState(ctx, c)
			}
		case e := <-truncation:
			// report immediately instead of waiting for the next interval; without connection the report expires,
			// the counter in the next state is still correct
			if c.IsConnected() {
				m.publishTruncation(c, e)
				m.publishState(ctx, c)
			}
		case <-ctx.Done():
			// sauberes Offline statt LWT-Warterei
			if c.IsConnected() {
				c.Publish(m.availTopic(), 1, true, "offline").WaitTimeout(3 * time.Second)
				c.Disconnect(500)
			}
			return
		case <-tick.C:
			if c.IsConnected() {
				m.publishState(ctx, c)
			}
		}
	}
}

func (m *MQTTModule) device() map[string]any {
	return map[string]any{
		"identifiers":  []string{m.cfg.DeviceID},
		"name":         m.cfg.DeviceName,
		"manufacturer": firstNonEmpty(m.cfg.Manufacturer, "skirnir-router"),
		"model":        firstNonEmpty(m.cfg.Model, "GPU-Knoten"),
		"sw_version":   version,
	}
}

// legacyObjIDs: Object IDs of older agent versions; their retained discovery configs are cleared on first sync,
// otherwise the entities would remain in HA as corpses.
var legacyObjIDs = []struct{ comp, objID string }{{"sensor", "local_assist_model"}, {"binary_sensor", "local_assist_structure"}}

// syncDiscovery aligns the retained discovery configs in the broker with what this node actually has:
// create missing ones, delete surplus ones with empty payload. Called on every connect and after every state cycle
// so a group can appear or disappear retroactively (e.g. alias pulled later).
func (m *MQTTModule) syncDiscovery(c paho.Client) {
	dev := m.device()
	var added, removed int
	for _, s := range sensorDefs {
		want, known := m.wanted(s.objID)
		if !known {
			continue
		}
		m.mu.Lock()
		have, decided := m.announced[s.objID] // missing entry = nothing sent yet, so delete once as well
		m.mu.Unlock()
		if decided && want == have {
			continue
		}
		topic := fmt.Sprintf("%s/%s/%s/%s/config", m.cfg.DiscoveryPrefix, s.comp, m.cfg.DeviceID, s.objID)
		if !want {
			c.Publish(topic, 1, true, "")
			removed++
		} else {
			c.Publish(topic, 1, true, m.discoveryPayload(s, dev))
			added++
		}
		m.mu.Lock()
		m.announced[s.objID] = want
		m.mu.Unlock()
	}
	m.mu.Lock()
	clearLegacy := !m.legacyCleared
	m.legacyCleared = true
	m.mu.Unlock()
	if clearLegacy {
		for _, l := range legacyObjIDs {
			c.Publish(fmt.Sprintf("%s/%s/%s/%s/config", m.cfg.DiscoveryPrefix, l.comp, m.cfg.DeviceID, l.objID), 1, true, "")
		}
	}
	if added > 0 || removed > 0 {
		m.log.Infof("mqtt: discovery updated (%s): %d created, %d removed", m.cfg.DeviceID, added, removed)
	}
}

func (m *MQTTModule) discoveryPayload(s sensorDef, dev map[string]any) []byte {
	cfg := map[string]any{
		"name":                  s.name,
		"unique_id":             m.cfg.DeviceID + "_" + s.objID,
		"default_entity_id":     s.comp + "." + m.cfg.DeviceID + "_" + s.entityName(),
		"state_topic":           m.stateTopic(),
		"value_template":        "{{ value_json." + s.field + " }}",
		"availability_topic":    m.availTopic(),
		"payload_available":     "online",
		"payload_not_available": "offline",
		"icon":                  s.icon,
		"device":                dev,
	}
	for k, v := range s.extra {
		cfg[k] = v
	}
	if s.comp == "binary_sensor" {
		cfg["payload_on"], cfg["payload_off"] = "ON", "OFF"
	}
	if s.comp == "event" { // HA reads event_type directly from the JSON payload of the event topic
		delete(cfg, "value_template")
		cfg["state_topic"] = m.eventTopic()
		if s.objID == "gpu_guard" {
			cfg["state_topic"] = m.guardTopic()
		}
	}
	if _, ok := cfg["json_attributes_template"]; ok {
		cfg["json_attributes_topic"] = m.stateTopic()
	}
	b, _ := json.Marshal(cfg)
	return b
}

// publishKuerzung sends an HA event per truncation (event_type + details as attributes).
func (m *MQTTModule) publishTruncation(c paho.Client, e Truncation) {
	p := map[string]any{"event_type": e.Type, "kind": e.Kind, "at": e.At.Format(time.RFC3339)}
	if e.Type == truncationInput {
		p["limit"], p["prompt"], p["truncated_to"] = e.Limit, e.Prompt, e.TruncatedTo
	} else {
		p["n_tokens"] = e.NTokens
	}
	b, _ := json.Marshal(p)
	c.Publish(m.eventTopic(), 1, false, b)
}

// kuerzungState: counter and last truncation for the regular state. "None" = HA sets the time sensor to unknown.
func (m *MQTTModule) truncationState(st map[string]any) {
	st["ollama_truncations"], st["ollama_last_truncation"] = 0, "None"
	if m.sup == nil {
		return
	}
	counts, l := m.sup.truncations.Snapshot()
	st["ollama_truncations"] = counts[truncationInput] + counts[truncationContextFull]
	d := map[string]any{truncationInput: counts[truncationInput], truncationContextFull: counts[truncationContextFull]}
	if l != nil {
		st["ollama_last_truncation"] = l.At.Format(time.RFC3339)
		d["type"], d["kind"] = l.Type, l.Kind
		if l.Type == truncationInput {
			d["limit"], d["prompt"], d["truncated_to"] = l.Limit, l.Prompt, l.TruncatedTo
		} else {
			d["n_tokens"] = l.NTokens
		}
	}
	st["truncation_details"] = d
}

// clearDiscovery deletes the retained discovery configs (verb mqtt-clear, e.g. for test devices).
func (m *MQTTModule) clearDiscovery(ctx context.Context) error {
	pw, err := m.password(ctx)
	if err != nil {
		return err
	}
	opts := paho.NewClientOptions().AddBroker(m.broker()).SetTLSConfig(&tls.Config{MinVersion: tls.VersionTLS12}).
		SetClientID("skirnir-agent-clear").SetUsername(m.cfg.Username).SetPassword(pw).SetConnectTimeout(10 * time.Second)
	c := paho.NewClient(opts)
	if t := c.Connect(); !t.WaitTimeout(15*time.Second) || t.Error() != nil {
		return fmt.Errorf("connect: %v", t.Error())
	}
	for _, s := range sensorDefs {
		c.Publish(fmt.Sprintf("%s/%s/%s/%s/config", m.cfg.DiscoveryPrefix, s.comp, m.cfg.DeviceID, s.objID), 1, true, "").WaitTimeout(3 * time.Second)
	}
	c.Publish(m.availTopic(), 1, true, "").WaitTimeout(3 * time.Second)
	c.Publish(m.stateTopic(), 1, true, "").WaitTimeout(3 * time.Second)
	c.Disconnect(500)
	return nil
}

// collect corresponds to the former Python script, with two changes:
// gaming_mode comes from the router response (busy) instead of the dead Gaming-Watcher,
// stt from its own child process instead of a port rate.
func (m *MQTTModule) collect(ctx context.Context) map[string]any {
	st := map[string]any{
		"gaming_mode": "OFF", "alias_model": "unbekannt", "alias_model_structure": "OFF",
		"ollama_loaded": 0, "vram_used_mb": 0, "ollama_loaded_names": "-", "ollama": "OFF", "stt": "OFF",
		"agent_version": version, "router_heartbeat": "OFF",
	}
	m.truncationState(st)
	m.sensorState(st)
	m.guardState(st)
	hs := m.hb.Status()
	if hs.OK {
		st["router_heartbeat"] = "ON"
	}
	if hs.State == "busy" {
		st["gaming_mode"] = "ON"
	}
	var show struct {
		Details struct {
			Family        string `json:"family"`
			ParameterSize string `json:"parameter_size"`
		} `json:"details"`
	}
	switch err := m.ollama(ctx, "/api/show", map[string]string{"model": m.cfg.AliasModel}, &show); {
	case err == nil:
		m.setApplies("alias", true)
		fam := show.Details.Family
		name := strings.TrimSpace(fam + " " + show.Details.ParameterSize)
		if name != "" {
			st["alias_model"] = name
		}
		st["alias_model_family"] = fam
		if !familiesWithoutStructure[fam] {
			st["alias_model_structure"] = "ON"
		}
	case isStatus(err, http.StatusNotFound):
		m.setApplies("alias", false) // Ollama responds but doesn't know the model -> the alias doesn't exist on this node
	default:
		// Ollama unreachable: don't decide, otherwise entities disappear on every Ollama restart
		m.log.Debugf("mqtt: api/show %s: %v", m.cfg.AliasModel, err)
	}
	var ps struct {
		Models []struct {
			Name     string `json:"name"`
			SizeVRAM int64  `json:"size_vram"`
		} `json:"models"`
	}
	if err := m.ollama(ctx, "/api/ps", nil, &ps); err == nil {
		st["ollama"] = "ON"
		st["ollama_loaded"] = len(ps.Models)
		var vram int64
		names := make([]string, 0, len(ps.Models))
		for _, x := range ps.Models {
			vram += x.SizeVRAM
			names = append(names, x.Name)
		}
		st["vram_used_mb"] = int(vram / 1048576)
		if len(names) > 0 {
			st["ollama_loaded_names"] = strings.Join(names, ", ")
		}
	}
	// STT exists only where an STT child process is configured; the port is the fallback if the service
	// runs manually instead of via the supervisor. Without a child, the entity is omitted - and the port attempt too.
	cs, hasChild := m.sup.Child(m.cfg.SttChild)
	m.setApplies("stt", hasChild)
	if hasChild && ((cs.Running && cs.Healthy) || portOpen(m.cfg.SttFallbackPort)) {
		st["stt"] = "ON"
	}
	return st
}

// sensorState: Additional sensors from the last GPU sample. The groups "gpu" and "gpuz" are created once they
// have had values, and never removed afterwards - if someone closes GPU-Z, the entities show "unknown"
// (null) instead of disappearing and reappearing on the next start.
func (m *MQTTModule) sensorState(st map[string]any) {
	var sens *GPUSensors
	if m.gpu != nil {
		if smp, _ := m.gpu.Last(); smp != nil {
			sens = smp.Sensors
		}
	}
	sens.mqttState(st)
	if sens.hasNVML() {
		m.setApplies("gpu", true)
	}
	if sens.hasGPUZ() {
		m.setApplies("gpuz", true)
	}
}

// guardState: State of the GPU guard for the regular State (Attributes: Source, Limits, Warnings, last event).
func (m *MQTTModule) guardState(st map[string]any) {
	g := m.guard.Status()
	st["gpu_guard_state"] = g.State
	st["gpu_guard_problem"] = map[bool]string{true: "ON", false: "OFF"}[g.Problem]
	st["gpu_guard_target_w"] = fval(g.TargetW)
	d := map[string]any{"source": g.Source, "reason": g.Reason, "warnings": g.Warnings, "high_load_s": g.HighLoadS,
		"limit_w": fval(g.LimitW), "default_limit_w": fval(g.DefaultLimitW), "target_w": fval(g.TargetW),
		"pin16_w": fval(g.Pin16W), "pin16_v": fval(g.Pin16V), "pin16_ref_v": fval(g.Pin16RefV), "throttle": g.Throttle}
	if e, n := m.guard.LastEvent(); e != nil {
		d["last_event"], d["last_event_at"], d["events"] = e.Type+": "+e.Msg, e.At.Format(time.RFC3339), n
	}
	if hs := m.hb.Status(); hs.GuardAck != nil {
		d["router_honors"] = *hs.GuardAck
	}
	st["gpu_guard_details"] = d
}

func (m *MQTTModule) publishGuardEvent(c paho.Client, e guardEvent) {
	b, _ := json.Marshal(map[string]any{"event_type": e.Type, "msg": e.Msg, "limit_w": e.LimitW, "at": e.At.Format(time.RFC3339)})
	c.Publish(m.guardTopic(), 1, false, b)
}

func (m *MQTTModule) publishState(ctx context.Context, c paho.Client) {
	st := m.collect(ctx)
	m.syncDiscovery(c) // a group may be added or removed during operation
	m.publishStateOf(c, st)
}

func (m *MQTTModule) publishStateOf(c paho.Client, st map[string]any) {
	b, _ := json.Marshal(st)
	t := c.Publish(m.stateTopic(), 0, true, b)
	if t.WaitTimeout(5*time.Second) && t.Error() != nil {
		m.setErr(t.Error().Error())
		return
	}
	m.mu.Lock()
	m.lastPub, m.lastErr = time.Now(), ""
	m.published++
	m.mu.Unlock()
}

func (m *MQTTModule) ollama(ctx context.Context, path string, payload any, out any) error {
	var req *http.Request
	if payload != nil {
		b, _ := json.Marshal(payload)
		req, _ = http.NewRequestWithContext(ctx, http.MethodPost, m.cfg.OllamaURL+path, strings.NewReader(string(b)))
		req.Header.Set("Content-Type", "application/json")
	} else {
		req, _ = http.NewRequestWithContext(ctx, http.MethodGet, m.cfg.OllamaURL+path, nil)
	}
	resp, err := m.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return statusError{resp.StatusCode}
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// statusError distinguishes "Ollama responded, but with an error code" from "Ollama was unreachable".
type statusError struct{ code int }

func (e statusError) Error() string { return fmt.Sprintf("HTTP %d", e.code) }

func isStatus(err error, code int) bool {
	var se statusError
	return errors.As(err, &se) && se.code == code
}

func portOpen(port int) bool {
	c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 1500*time.Millisecond)
	if err != nil {
		return false
	}
	c.Close()
	return true
}

func (m *MQTTModule) setErr(s string) { m.mu.Lock(); m.lastErr = s; m.mu.Unlock() }

type MQTTStatus struct {
	Connected bool   `json:"connected"`
	LastPub   string `json:"last_publish,omitempty"`
	Published int    `json:"published"`
	LastError string `json:"last_error,omitempty"`
	DeviceID  string `json:"device_id"`
}

func (m *MQTTModule) Status() MQTTStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	s := MQTTStatus{Connected: m.connected, Published: m.published, LastError: m.lastErr, DeviceID: m.cfg.DeviceID}
	if !m.lastPub.IsZero() {
		s.LastPub = m.lastPub.Format(time.RFC3339)
	}
	return s
}
