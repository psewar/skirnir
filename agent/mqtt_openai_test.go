package main

import (
	"context"
	"net/http/httptest"
	"testing"
	"time"
)

// OpenAI-compatible upstream: the Home Assistant entities come from the watcher's report (/v1/models), "ollama" is ON
// only while the server answers, and the alias group does not apply (an alias is an Ollama model name).
func TestMQTTOpenAIState(t *testing.T) {
	f := &fakeOpenAI{}
	f.set(`{"id":"big-moe","status":{"value":"loaded"}}`)
	srv := httptest.NewServer(f)
	defer srv.Close()
	m := testModule(t)
	m.upstream = newOllamaWatcher(srv.URL, upstreamOpenAI, quietLogger())

	st := map[string]any{"ollama": "OFF", "ollama_loaded": 0, "ollama_loaded_names": "-"}
	m.openAIState(st)
	if st["ollama"] != "OFF" || m.applies["alias"] {
		t.Fatalf("no report yet: ollama OFF, alias not applicable: %v %v", st, m.applies)
	}
	if applies, known := m.applies["alias"]; !known || applies {
		t.Fatal("alias group decided as not applicable on an OpenAI node")
	}

	m.upstream.poll(context.Background(), time.Now())
	m.openAIState(st)
	if st["ollama"] != "ON" || st["ollama_loaded"] != 1 || st["ollama_loaded_names"] != "big-moe" {
		t.Fatalf("answering server: ON with its model: %v", st)
	}

	m.upstream.SetHeld(true, time.Now())
	st = map[string]any{"ollama": "OFF"}
	m.openAIState(st)
	if st["ollama"] != "OFF" {
		t.Fatalf("held for the GPU: ollama OFF: %v", st)
	}
}
