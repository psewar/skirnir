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

// MQTTModule ersetzt das fruehere Python-Skript: gleiches Geraet, gleiche unique_ids, gleiche Topics,
// damit die HA-Entitaeten und die Cloud-Fallback-Automation unveraendert weiterlaufen.
type MQTTModule struct {
	cfg           MQTTCfg
	secretStore   SecretStoreCfg
	legacyCleared bool // Discovery-Configs frueherer Object-IDs einmal je Lauf geleert
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
	applies   map[string]bool // optionale Entitaetsgruppe -> gilt auf diesem Knoten (fehlt = noch unbekannt)
	announced map[string]bool // objID -> Discovery-Config steht im Broker
}

// Familien, deren Modelle HAs ai_task-Structured-Output nicht koennen (Beleg 2026-08-23).
var familiesWithoutStructure = map[string]bool{"gptoss": true}

type sensorDef struct {
	comp, objID, name, field, icon string
	extra                          map[string]any
}

// 1:1 aus dem frueheren Python-Skript.
var sensorDefs = []sensorDef{
	{"binary_sensor", "stt", "STT-Dienst", "stt", "mdi:microphone-message", nil},
	{"binary_sensor", "ollama", "Ollama", "ollama", "mdi:robot", nil},
	{"binary_sensor", "gaming_mode", "Gaming-Modus", "gaming_mode", "mdi:controller", nil},
	{"binary_sensor", "alias_model_structure", "Alias-Modell strukturfähig", "alias_model_structure", "mdi:code-json", nil},
	{"sensor", "alias_model", "Alias-Modell", "alias_model", "mdi:tag-text", nil},
	{"sensor", "vram_used_mb", "VRAM belegt", "vram_used_mb", "mdi:memory", map[string]any{"unit_of_measurement": "MB", "state_class": "measurement"}},
	{"sensor", "ollama_loaded", "Ollama geladene Modelle", "ollama_loaded", "mdi:layers", map[string]any{"state_class": "measurement"}},
	{"sensor", "ollama_loaded_names", "Ollama geladen", "ollama_loaded_names", "mdi:format-list-bulleted", nil},
	// neu: Sicht des Dienstes selbst
	{"sensor", "agent_version", "Agent Version", "agent_version", "mdi:package-variant", map[string]any{"entity_category": "diagnostic"}},
	{"binary_sensor", "router_heartbeat", "Router-Heartbeat", "router_heartbeat", "mdi:heart-pulse", map[string]any{"entity_category": "diagnostic"}},
	// Stille Kuerzungen im Ollama-Log (kuerzung.go): Zaehler seit Dienststart, Zeitpunkt + Details der letzten,
	// und ein Ereignis je Kuerzung fuer Automationen (Topic <device>/kuerzung, nicht retained).
	{"sensor", "ollama_kuerzungen", "Ollama Kürzungen", "ollama_kuerzungen", "mdi:content-cut", map[string]any{"state_class": "total_increasing"}},
	{"sensor", "ollama_letzte_kuerzung", "Ollama letzte Kürzung", "ollama_letzte_kuerzung", "mdi:clock-alert-outline",
		map[string]any{"device_class": "timestamp", "json_attributes_template": "{{ value_json.kuerzung_details | tojson }}"}},
	{"event", "ollama_kuerzung", "Ollama Kürzung", "", "mdi:content-cut",
		map[string]any{"event_types": []string{kuerzungEingabe, kuerzungKontextVoll}}},
	// Sensoren (0.6.0). Gruppe "gpu": NVML bzw. nvidia-smi; Gruppe "gpuz": nur wo GPU-Z einmal gesehen wurde.
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
	// GPU-Schutz (0.7.0, guard.go): Zustand mit Attributen, Problem-Sensor, Ereignisse auf <device>/gpu_guard
	{"sensor", "gpu_guard_state", "GPU-Schutz", "gpu_guard_state", "mdi:shield-half-full",
		map[string]any{"json_attributes_template": "{{ value_json.gpu_guard_details | tojson }}"}},
	{"binary_sensor", "gpu_guard_problem", "GPU-Schutz Problem", "gpu_guard_problem", "mdi:shield-alert", map[string]any{"device_class": "problem"}},
	{"sensor", "gpu_guard_target_w", "GPU-Schutz Ziel-Limit", "gpu_guard_target_w", "mdi:shield-check", map[string]any{"unit_of_measurement": "W", "device_class": "power", "entity_category": "diagnostic"}},
	{"event", "gpu_guard", "GPU-Schutz Ereignis", "", "mdi:shield-alert",
		map[string]any{"event_types": []string{guardEvHochlast, guardEvGedrosselt, guardEvErholung, guardEvLimitGesetzt, guardEvLimitVerweigert, guardEvFremdeingriff, guardEvSpannung, guardEvTemperatur, guardEvHwDrossel, guardEvAbgewaehlt}}},
}

var (
	tempExtra  = map[string]any{"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}
	powerExtra = map[string]any{"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"}
)

// Entitaeten, die es nicht auf jedem Knoten gibt: objID -> Gruppe. Eine Gruppe wird nur angelegt, wenn sie auf
// diesem Rechner etwas bedeutet - sonst haengen auf fremden Knoten Sensoren im HA, die ewig OFF stehen.
//
//	stt   = ein Kindprozess mit dem Namen mqtt.stt_child ist konfiguriert
//	alias = das Modell mqtt.alias_model liegt in der lokalen Ollama (leer = Gruppe aus)
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

// setApplies merkt, ob eine optionale Gruppe auf diesem Knoten gilt. Nur mit sicherer Auskunft aufrufen:
// "Ollama war nicht erreichbar" darf keine Entitaet loeschen.
func (m *MQTTModule) setApplies(group string, yes bool) {
	m.mu.Lock()
	m.applies[group] = yes
	m.mu.Unlock()
}

// wanted sagt, ob eine Entitaet auf diesem Knoten existieren soll. Solange eine Gruppe unbekannt ist, wird nichts
// angelegt und nichts geloescht (zweiter Rueckgabewert false).
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
func (m *MQTTModule) eventTopic() string { return m.cfg.DeviceID + "/kuerzung" }
func (m *MQTTModule) guardTopic() string { return m.cfg.DeviceID + "/gpu_guard" }

func (m *MQTTModule) password(ctx context.Context) (string, error) {
	if m.cfg.Password != "" {
		return m.cfg.Password, nil
	}
	return fetchSecret(ctx, m.secretStore, m.cfg.PasswordSecret)
}

func (m *MQTTModule) Run(ctx context.Context) {
	// Passwort holen, bei Ausfall des Secret-Stores geduldig wiederholen (der Dienst darf deswegen nicht sterben).
	var pw string
	for delay := 5 * time.Second; ; delay = min(delay*2, 5*time.Minute) {
		var err error
		if pw, err = m.password(ctx); err == nil {
			break
		}
		m.setErr("Passwort: " + err.Error())
		m.log.Warnf("mqtt: %v, neuer Versuch in %s", err, delay)
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
			m.announced = map[string]bool{} // nach einem Reconnect die retained Configs neu abgleichen
			m.mu.Unlock()
			m.log.Infof("mqtt: verbunden mit %s", m.broker())
			st := m.collect(ctx) // erst nachsehen, was es hier gibt, dann die passenden Entitaeten anlegen
			m.syncDiscovery(c)
			c.Publish(m.availTopic(), 1, true, "online")
			m.publishStateOf(c, st)
		}).
		SetConnectionLostHandler(func(c paho.Client, err error) {
			m.mu.Lock()
			m.connected = false
			m.mu.Unlock()
			m.setErr(err.Error())
			m.log.Warnf("mqtt: Verbindung verloren: %v", err)
		})
	c := paho.NewClient(opts)
	m.mu.Lock()
	m.client = c
	m.mu.Unlock()
	if t := c.Connect(); t.WaitTimeout(15*time.Second) && t.Error() != nil {
		m.log.Warnf("mqtt: erster Connect: %v (Retry laeuft)", t.Error())
	}
	tick := time.NewTicker(time.Duration(m.cfg.IntervalS * float64(time.Second)))
	defer tick.Stop()
	var kuerzung <-chan Kuerzung
	if m.sup != nil {
		kuerzung = m.sup.kuerzungen.neu
	}
	var guardEv <-chan guardEvent
	if m.guard != nil {
		guardEv = m.guard.events
	}
	for {
		select {
		case e := <-guardEv: // GPU-Schutz-Ereignis sofort melden, Zustand gleich mit
			if c.IsConnected() {
				m.publishGuardEvent(c, e)
				m.publishState(ctx, c)
			}
		case e := <-kuerzung:
			// sofort melden statt bis zum naechsten Intervall zu warten; ohne Verbindung verfaellt die Meldung,
			// der Zaehler im naechsten State stimmt trotzdem
			if c.IsConnected() {
				m.publishKuerzung(c, e)
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

// legacyObjIDs: Object-IDs aelterer Agent-Versionen; ihre retained Discovery-Configs werden beim ersten Sync geleert,
// sonst blieben die Entitaeten in HA als Leichen stehen.
var legacyObjIDs = []struct{ comp, objID string }{{"sensor", "local_assist_model"}, {"binary_sensor", "local_assist_structure"}}

// syncDiscovery bringt die retained Discovery-Configs im Broker mit dem in Deckung, was dieser Knoten wirklich hat:
// fehlende anlegen, ueberzaehlige mit leerem Payload loeschen. Wird bei jedem Connect und nach jedem State-Zyklus
// aufgerufen, damit eine Gruppe auch nachtraeglich auftauchen oder verschwinden darf (z. B. Alias nachtraeglich gepullt).
func (m *MQTTModule) syncDiscovery(c paho.Client) {
	dev := m.device()
	var added, removed int
	for _, s := range sensorDefs {
		want, known := m.wanted(s.objID)
		if !known {
			continue
		}
		m.mu.Lock()
		have, decided := m.announced[s.objID] // fehlender Eintrag = noch nichts geschickt, also auch einmal loeschen
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
		m.log.Infof("mqtt: Discovery aktualisiert (%s): %d angelegt, %d entfernt", m.cfg.DeviceID, added, removed)
	}
}

func (m *MQTTModule) discoveryPayload(s sensorDef, dev map[string]any) []byte {
	cfg := map[string]any{
		"name":                  s.name,
		"unique_id":             m.cfg.DeviceID + "_" + s.objID,
		"object_id":             m.cfg.DeviceID + "_" + s.objID,
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
	if s.comp == "event" { // HA liest event_type direkt aus dem JSON-Payload des Ereignis-Topics
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

// publishKuerzung schickt ein HA-Ereignis je Kuerzung (event_type + Details als Attribute).
func (m *MQTTModule) publishKuerzung(c paho.Client, e Kuerzung) {
	p := map[string]any{"event_type": e.Art, "kind": e.Kind, "zeit": e.Zeit.Format(time.RFC3339)}
	if e.Art == kuerzungEingabe {
		p["limit"], p["prompt"], p["neu"] = e.Limit, e.Prompt, e.Neu
	} else {
		p["n_tokens"] = e.NTokens
	}
	b, _ := json.Marshal(p)
	c.Publish(m.eventTopic(), 1, false, b)
}

// kuerzungState: Zaehler und letzte Kuerzung fuer den regulaeren State. "None" = HA setzt den Zeitsensor auf unbekannt.
func (m *MQTTModule) kuerzungState(st map[string]any) {
	st["ollama_kuerzungen"], st["ollama_letzte_kuerzung"] = 0, "None"
	if m.sup == nil {
		return
	}
	anzahl, l := m.sup.kuerzungen.Stand()
	st["ollama_kuerzungen"] = anzahl[kuerzungEingabe] + anzahl[kuerzungKontextVoll]
	d := map[string]any{kuerzungEingabe: anzahl[kuerzungEingabe], kuerzungKontextVoll: anzahl[kuerzungKontextVoll]}
	if l != nil {
		st["ollama_letzte_kuerzung"] = l.Zeit.Format(time.RFC3339)
		d["art"], d["kind"] = l.Art, l.Kind
		if l.Art == kuerzungEingabe {
			d["limit"], d["prompt"], d["neu"] = l.Limit, l.Prompt, l.Neu
		} else {
			d["n_tokens"] = l.NTokens
		}
	}
	st["kuerzung_details"] = d
}

// clearDiscovery loescht die retained Discovery-Configs (Verb mqtt-clear, z. B. fuer Test-Geraete).
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

// collect entspricht dem frueheren Python-Skript, mit zwei Aenderungen:
// gaming_mode kommt aus der Router-Antwort (busy) statt aus dem toten Gaming-Watcher,
// stt aus dem eigenen Kindprozess statt aus einem Port-Raten.
func (m *MQTTModule) collect(ctx context.Context) map[string]any {
	st := map[string]any{
		"gaming_mode": "OFF", "alias_model": "unbekannt", "alias_model_structure": "OFF",
		"ollama_loaded": 0, "vram_used_mb": 0, "ollama_loaded_names": "-", "ollama": "OFF", "stt": "OFF",
		"agent_version": version, "router_heartbeat": "OFF",
	}
	m.kuerzungState(st)
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
		m.setApplies("alias", false) // Ollama antwortet und kennt das Modell nicht -> auf diesem Knoten gibt es den Alias nicht
	default:
		// Ollama nicht erreichbar: nichts entscheiden, sonst verschwinden Entitaeten bei jedem Ollama-Neustart
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
	// STT gibt es nur, wo ein STT-Kindprozess konfiguriert ist; der Port ist der Rueckfallweg, falls der Dienst
	// von Hand statt ueber den Supervisor laeuft. Ohne Kind entfaellt die Entitaet - und der Portversuch gleich mit.
	cs, hasChild := m.sup.Child(m.cfg.SttChild)
	m.setApplies("stt", hasChild)
	if hasChild && ((cs.Running && cs.Healthy) || portOpen(m.cfg.SttFallbackPort)) {
		st["stt"] = "ON"
	}
	return st
}

// sensorState: Zusatzsensoren aus dem letzten GPU-Sample. Die Gruppen "gpu" und "gpuz" werden angelegt, sobald sie
// einmal Werte hatten, und danach nicht mehr entfernt - schliesst jemand GPU-Z, zeigen die Entitaeten "unbekannt"
// (null) statt zu verschwinden und beim naechsten Start wieder aufzutauchen.
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

// guardState: Zustand des GPU-Schutzes fuer den regulaeren State (Attribute: Quelle, Limits, Warnungen, letztes Ereignis).
func (m *MQTTModule) guardState(st map[string]any) {
	g := m.guard.Status()
	st["gpu_guard_state"] = g.State
	st["gpu_guard_problem"] = map[bool]string{true: "ON", false: "OFF"}[g.Problem]
	st["gpu_guard_target_w"] = fval(g.TargetW)
	d := map[string]any{"quelle": g.Source, "grund": g.Reason, "warnungen": g.Warnings, "hochlast_s": g.HighLoadS,
		"limit_w": fval(g.LimitW), "default_limit_w": fval(g.DefaultLimitW), "target_w": fval(g.TargetW),
		"pin16_w": fval(g.Pin16W), "pin16_v": fval(g.Pin16V), "pin16_ref_v": fval(g.Pin16RefV), "throttle": g.Throttle}
	if e, n := m.guard.LastEvent(); e != nil {
		d["letztes_ereignis"], d["letztes_ereignis_zeit"], d["ereignisse"] = e.Type+": "+e.Msg, e.At.Format(time.RFC3339), n
	}
	if hs := m.hb.Status(); hs.GuardAck != nil {
		d["router_beachtet"] = *hs.GuardAck
	}
	st["gpu_guard_details"] = d
}

func (m *MQTTModule) publishGuardEvent(c paho.Client, e guardEvent) {
	b, _ := json.Marshal(map[string]any{"event_type": e.Type, "msg": e.Msg, "limit_w": e.LimitW, "zeit": e.At.Format(time.RFC3339)})
	c.Publish(m.guardTopic(), 1, false, b)
}

func (m *MQTTModule) publishState(ctx context.Context, c paho.Client) {
	st := m.collect(ctx)
	m.syncDiscovery(c) // eine Gruppe darf im Betrieb dazukommen oder wegfallen
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

// statusError trennt "Ollama hat geantwortet, aber mit Fehlercode" von "Ollama war nicht erreichbar".
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
