package main

import (
	"strings"
	"testing"

	paho "github.com/eclipse/paho.mqtt.golang"
)

// fakeClient only records what was published; everything else is scaffolding for paho.Client.
type fakeClient struct {
	pubs []struct{ topic, payload string }
}

func (f *fakeClient) Publish(topic string, _ byte, _ bool, payload any) paho.Token {
	s := ""
	switch p := payload.(type) {
	case string:
		s = p
	case []byte:
		s = string(p)
	}
	f.pubs = append(f.pubs, struct{ topic, payload string }{topic, s})
	return nil
}
func (f *fakeClient) IsConnected() bool                                      { return true }
func (f *fakeClient) IsConnectionOpen() bool                                 { return true }
func (f *fakeClient) Connect() paho.Token                                    { return nil }
func (f *fakeClient) Disconnect(uint)                                        {}
func (f *fakeClient) Subscribe(string, byte, paho.MessageHandler) paho.Token { return nil }
func (f *fakeClient) SubscribeMultiple(map[string]byte, paho.MessageHandler) paho.Token {
	return nil
}
func (f *fakeClient) Unsubscribe(...string) paho.Token        { return nil }
func (f *fakeClient) AddRoute(string, paho.MessageHandler)    {}
func (f *fakeClient) OptionsReader() paho.ClientOptionsReader { return paho.ClientOptionsReader{} }

// last returns the last published payload for an objID (or "" if never sent).
func (f *fakeClient) last(objID string) (string, bool) {
	out, ok := "", false
	for _, p := range f.pubs {
		if strings.HasSuffix(p.topic, "/"+objID+"/config") {
			out, ok = p.payload, true
		}
	}
	return out, ok
}

func testModule(t *testing.T) *MQTTModule {
	t.Helper()
	log, err := newLogger(LogCfg{Dir: t.TempDir()}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	// Windows does not delete open files: without closing, TempDir cleanup fails.
	t.Cleanup(func() { log.file.Close() })
	return newMQTT(MQTTCfg{DeviceID: "testknoten", DeviceName: "Testknoten", DiscoveryPrefix: "homeassistant"},
		SecretStoreCfg{}, "testknoten", log, nil, nil, nil, nil)
}

func TestSyncDiscoveryWithoutGroups(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.syncDiscovery(c)
	// Until a group is decided: create mandatory entities, leave optional ones untouched.
	if _, ok := c.last("ollama"); !ok {
		t.Fatal("ollama haette angelegt werden muessen")
	}
	for _, obj := range []string{"stt", "alias_model", "alias_model_structure"} {
		if _, ok := c.last(obj); ok {
			t.Fatalf("%s wurde angefasst, obwohl die Gruppe noch unbekannt ist", obj)
		}
	}
}

func TestSyncDiscoveryForeignNode(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.setApplies("stt", false)   // no STT child process configured
	m.setApplies("alias", false) // Ollama does not know the alias
	m.syncDiscovery(c)
	for _, obj := range []string{"stt", "alias_model", "alias_model_structure"} {
		p, ok := c.last(obj)
		if !ok {
			t.Fatalf("%s: kein Publish, die alte retained Config bliebe stehen", obj)
		}
		if p != "" {
			t.Fatalf("%s: erwartet leeres Payload (Loeschung), bekam %q", obj, p)
		}
	}
	if p, _ := c.last("ollama"); p == "" {
		t.Fatal("ollama darf nicht geloescht werden")
	}
}

func TestSyncDiscoveryOwnNode(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.setApplies("stt", true)
	m.setApplies("alias", true)
	m.syncDiscovery(c)
	for _, obj := range []string{"stt", "alias_model", "alias_model_structure"} {
		p, ok := c.last(obj)
		if !ok || p == "" {
			t.Fatalf("%s haette angelegt werden muessen", obj)
		}
		if !strings.Contains(p, "\"unique_id\":\"testknoten_"+obj+"\"") {
			t.Fatalf("%s: unique_id fehlt im Payload: %s", obj, p)
		}
	}
}

func TestSyncDiscoveryOnceAndLater(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.setApplies("stt", false)
	m.setApplies("alias", false)
	m.syncDiscovery(c)
	n := len(c.pubs)
	m.syncDiscovery(c) // nothing changed -> no further publish
	if len(c.pubs) != n {
		t.Fatalf("zweiter Lauf hat %d zusaetzliche Publishes erzeugt", len(c.pubs)-n)
	}
	m.setApplies("alias", true) // Alias pulled retroactively
	m.syncDiscovery(c)
	if p, ok := c.last("alias_model"); !ok || p == "" {
		t.Fatal("alias_model haette nachtraeglich angelegt werden muessen")
	}
	if p, _ := c.last("stt"); p != "" {
		t.Fatal("stt haette geloescht bleiben muessen")
	}
}

// Truncation message (truncation.go): Discovery of the event and time sensor, event payload, state fields.
func TestTruncationDiscovery(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.syncDiscovery(c)
	ev, ok := c.last("ollama_kuerzung")
	if !ok || ev == "" {
		t.Fatal("Ereignis-Entitaet nicht angelegt")
	}
	for _, wantPart := range []string{`"state_topic":"testknoten/truncation"`, `"event_types":["input_truncated","context_full"]`,
		// unique_id remains the name from 2026 (history), the entity ID is English (0.20.0)
		`"unique_id":"testknoten_ollama_kuerzung"`, `"default_entity_id":"event.testknoten_ollama_truncation"`} {
		if !strings.Contains(ev, wantPart) {
			t.Fatalf("Ereignis-Config ohne %s: %s", wantPart, ev)
		}
	}
	if strings.Contains(ev, "value_template") {
		t.Fatalf("Ereignis darf kein value_template haben (HA liest event_type direkt): %s", ev)
	}
	ts, _ := c.last("ollama_letzte_kuerzung")
	for _, wantPart := range []string{`"device_class":"timestamp"`, `"json_attributes_topic":"testknoten/state"`} {
		if !strings.Contains(ts, wantPart) {
			t.Fatalf("Zeitsensor ohne %s: %s", wantPart, ts)
		}
	}
	if z, _ := c.last("ollama_kuerzungen"); !strings.Contains(z, `"state_class":"total_increasing"`) {
		t.Fatalf("Zaehler ohne total_increasing: %s", z)
	}
}

func TestTruncationEventAndState(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.sup = &Supervisor{truncations: newTruncations()}
	st := map[string]any{}
	m.truncationState(st)
	if st["ollama_truncations"] != 0 || st["ollama_last_truncation"] != "None" {
		t.Fatalf("ohne Kuerzung: %v", st)
	}
	m.sup.truncations.inspect("ollama", lineInput)
	e := <-m.sup.truncations.fresh
	m.publishTruncation(c, e)
	p := c.pubs[len(c.pubs)-1]
	if p.topic != "testknoten/truncation" || !strings.Contains(p.payload, `"event_type":"input_truncated"`) ||
		!strings.Contains(p.payload, `"prompt":65536`) {
		t.Fatalf("Ereignis falsch: %s %s", p.topic, p.payload)
	}
	m.truncationState(st)
	d, _ := st["truncation_details"].(map[string]any)
	if st["ollama_truncations"] != 1 || st["ollama_last_truncation"] == "None" || d["limit"] != 32770 || d["type"] != truncationInput {
		t.Fatalf("State nach Kuerzung: %v", st)
	}
}
