package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeOllama spielt /api/ps und /api/tags mit austauschbarem Inhalt; down = 500 auf alles.
type fakeOllama struct {
	mu   sync.Mutex
	ps   string
	tags string
	down bool
}

func (f *fakeOllama) set(ps, tags string, down bool) {
	f.mu.Lock()
	f.ps, f.tags, f.down = ps, tags, down
	f.mu.Unlock()
}

func (f *fakeOllama) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.down {
		http.Error(w, "kaputt", http.StatusInternalServerError)
		return
	}
	switch r.URL.Path {
	case "/api/ps":
		w.Write([]byte(`{"models":[` + f.ps + `]}`))
	case "/api/tags":
		w.Write([]byte(`{"models":[` + f.tags + `]}`))
	default:
		http.NotFound(w, r)
	}
}

func TestOllamaWatcher(t *testing.T) {
	f := &fakeOllama{}
	srv := httptest.NewServer(f)
	defer srv.Close()
	w := newOllamaWatcher(srv.URL, quietLogger())
	w.tagsEvery = 0 // jede Runde auch /api/tags
	ctx := context.Background()
	now := time.Now()
	qwen := `{"name":"qwen:27b","digest":"aa","size":1}`
	loaded := func(exp string) string {
		return `{"name":"qwen:27b","digest":"aa","size_vram":20,"context_length":131072,"expires_at":"` + exp + `"}`
	}

	if st, have := w.Snapshot(); have || st.Rev != 0 {
		t.Fatalf("vor der ersten Abfrage nichts meldbar: %+v %v", st, have)
	}
	f.set("", qwen, false)
	w.poll(ctx, now)
	st, have := w.Snapshot()
	if !have || !st.Up || st.Rev != 1 || len(st.Tags) != 1 || len(st.PS) != 0 {
		t.Fatalf("erste Abfrage: %+v %v", st, have)
	}
	// nichts geladen: die Nutzlast muss eine leere Liste tragen, nicht null (0.14.0 schickte "ps":null)
	if b, _ := json.Marshal(st); !strings.Contains(string(b), `"ps":[]`) || !strings.Contains(string(b), `"tags":[{`) {
		t.Fatalf("leere Liste als [] erwartet: %s", b)
	}
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.Rev != 1 {
		t.Fatalf("ohne Aenderung keine neue Revision: %d", st.Rev)
	}
	f.set(loaded("2026-09-30T20:00:00Z"), qwen, false)
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.Rev != 2 || len(st.PS) != 1 {
		t.Fatalf("Modell geladen -> Revision 2: %+v", st)
	}
	f.set(loaded("2026-09-30T20:05:00Z"), qwen, false)
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.Rev != 2 {
		t.Fatalf("nur expires_at geaendert -> keine neue Revision: %d", st.Rev)
	}
	f.set(loaded("2026-09-30T20:05:00Z"), qwen+`,{"name":"granite:8b","digest":"bb","size":2}`, false)
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.Rev != 3 || len(st.Tags) != 2 {
		t.Fatalf("neues Modell installiert -> Revision 3: %+v", st)
	}
	f.set("", "", true)
	w.poll(ctx, now)
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); !st.Up || st.Rev != 3 {
		t.Fatalf("zwei Fehlschlaege: noch erreichbar gemeldet: %+v", st)
	}
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.Up || st.Rev != 4 || st.Error == "" {
		t.Fatalf("dritter Fehlschlag -> nicht erreichbar mit Grund: %+v", st)
	}
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); st.Rev != 4 {
		t.Fatalf("weiter nicht erreichbar: keine neue Revision: %d", st.Rev)
	}
	f.set("", qwen, false)
	w.poll(ctx, now)
	if st, _ = w.Snapshot(); !st.Up || st.Rev != 5 || st.Error != "" {
		t.Fatalf("wieder erreichbar -> Revision 5: %+v", st)
	}
	b, _ := json.Marshal(st)
	var back map[string]any
	if json.Unmarshal(b, &back) != nil || back["up"] != true || back["tags"] == nil || back["ps"] == nil {
		t.Fatalf("Nutzlast: %s", b)
	}
}

func TestOllamaWatcherDownFromStart(t *testing.T) {
	f := &fakeOllama{down: true}
	srv := httptest.NewServer(f)
	defer srv.Close()
	w := newOllamaWatcher(srv.URL, quietLogger())
	for i := 0; i < 3; i++ {
		w.poll(context.Background(), time.Now())
	}
	st, have := w.Snapshot()
	if !have || st.Up {
		t.Fatalf("von Anfang an nicht erreichbar -> nach drei Versuchen meldbar als down: %+v %v", st, have)
	}
	if b, _ := json.Marshal(st); strings.Contains(string(b), "null") {
		t.Fatalf("auch im Zustand down keine null-Listen: %s", b)
	}
	// Ollama kommt hoch, nichts geladen und nichts installiert: erste Antwort ist eine Aenderung, beide Listen []
	f.set("", "", false)
	w.poll(context.Background(), time.Now())
	st, _ = w.Snapshot()
	if b, _ := json.Marshal(st); !st.Up || !strings.Contains(string(b), `"ps":[]`) || !strings.Contains(string(b), `"tags":[]`) {
		t.Fatalf("nach dem Hochfahren up mit leeren Listen erwartet: %s", b)
	}
	if newOllamaWatcher("", quietLogger()) != nil {
		t.Fatal("ohne Upstream kein Beobachter")
	}
}
