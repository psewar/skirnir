package main

import (
	"strings"
	"testing"

	paho "github.com/eclipse/paho.mqtt.golang"
)

// fakeClient merkt sich nur, was veroeffentlicht wurde; alles andere ist Kulisse fuer paho.Client.
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

// last liefert das zuletzt veroeffentlichte Payload zu einer objID (oder "", wenn nie geschickt).
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
	return newMQTT(MQTTCfg{DeviceID: "testknoten", DeviceName: "Testknoten", DiscoveryPrefix: "homeassistant"},
		SecretStoreCfg{}, "testknoten", log, nil, nil, nil)
}

func TestSyncDiscoveryOhneGruppen(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.syncDiscovery(c)
	// Solange keine Gruppe entschieden ist: Pflichtentitaeten anlegen, optionale unangetastet lassen.
	if _, ok := c.last("ollama"); !ok {
		t.Fatal("ollama haette angelegt werden muessen")
	}
	for _, obj := range []string{"stt", "alias_model", "alias_model_structure"} {
		if _, ok := c.last(obj); ok {
			t.Fatalf("%s wurde angefasst, obwohl die Gruppe noch unbekannt ist", obj)
		}
	}
}

func TestSyncDiscoveryFremderKnoten(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.setApplies("stt", false)   // kein STT-Kindprozess konfiguriert
	m.setApplies("alias", false) // Ollama kennt den Alias nicht
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

func TestSyncDiscoveryEigenerKnoten(t *testing.T) {
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

func TestSyncDiscoveryNurEinmalUndNachtraeglich(t *testing.T) {
	m, c := testModule(t), &fakeClient{}
	m.setApplies("stt", false)
	m.setApplies("alias", false)
	m.syncDiscovery(c)
	n := len(c.pubs)
	m.syncDiscovery(c) // nichts geaendert -> kein weiteres Publish
	if len(c.pubs) != n {
		t.Fatalf("zweiter Lauf hat %d zusaetzliche Publishes erzeugt", len(c.pubs)-n)
	}
	m.setApplies("alias", true) // Alias nachtraeglich gepullt
	m.syncDiscovery(c)
	if p, ok := c.last("alias_model"); !ok || p == "" {
		t.Fatal("alias_model haette nachtraeglich angelegt werden muessen")
	}
	if p, _ := c.last("stt"); p != "" {
		t.Fatal("stt haette geloescht bleiben muessen")
	}
}
