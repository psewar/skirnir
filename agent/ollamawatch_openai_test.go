package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// fakeOpenAI serves /v1/models of an OpenAI-compatible server; /api/* is unknown there (404), as on a real one.
type fakeOpenAI struct {
	mu     sync.Mutex
	models string
}

func (f *fakeOpenAI) set(models string) {
	f.mu.Lock()
	f.models = models
	f.mu.Unlock()
}

func (f *fakeOpenAI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.URL.Path != "/v1/models" {
		http.NotFound(w, r)
		return
	}
	w.Write([]byte(`{"object":"list","data":[` + f.models + `]}`))
}

func entry(t *testing.T, raw json.RawMessage) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("entry is not JSON: %s", raw)
	}
	return m
}

func TestOllamaWatcherOpenAIUpstream(t *testing.T) {
	f := &fakeOpenAI{}
	srv := httptest.NewServer(f)
	defer srv.Close()
	w := newOllamaWatcher(srv.URL, upstreamOpenAI, quietLogger())
	if w.every != 5*time.Second || w.client.Timeout != 10*time.Second {
		t.Fatalf("OpenAI upstream: slower poll and longer timeout expected, got %v / %v", w.every, w.client.Timeout)
	}
	ctx := context.Background()

	// A server with extras (status, meta.n_ctx): one installed and loaded model with its context length.
	f.set(`{"id":"big-moe","object":"model","status":{"value":"loaded"},"meta":{"n_ctx":131072}}`)
	w.poll(ctx, time.Now())
	st, have := w.Snapshot()
	if !have || !st.Up || st.Rev != 1 || len(st.Tags) != 1 || len(st.PS) != 1 {
		t.Fatalf("first poll: %+v %v", st, have)
	}
	tag, ps := entry(t, st.Tags[0]), entry(t, st.PS[0])
	if tag["name"] != "big-moe" || tag["digest"] != "openai:big-moe" {
		t.Fatalf("tag entry: %v", tag)
	}
	if ps["name"] != "big-moe" || ps["digest"] != "openai:big-moe" || ps["context_length"] != float64(131072) || ps["size_vram"] != float64(0) {
		t.Fatalf("ps entry (size_vram 0: memory counts once, as child): %v", ps)
	}

	w.poll(ctx, time.Now())
	if st, _ = w.Snapshot(); st.Rev != 1 {
		t.Fatalf("unchanged list -> no new revision: %d", st.Rev)
	}

	// A plain OpenAI server: no status, no context -> loaded, without context_length.
	f.set(`{"id":"plain","object":"model"}`)
	w.poll(ctx, time.Now())
	st, _ = w.Snapshot()
	if st.Rev != 2 || len(st.PS) != 1 {
		t.Fatalf("model without status counts as loaded: %+v", st)
	}
	if _, has := entry(t, st.PS[0])["context_length"]; has {
		t.Fatalf("no context length reported -> none invented: %s", st.PS[0])
	}

	// A model the server lists but has not loaded: installed, not in ps.
	f.set(`{"id":"cold","object":"model","status":{"value":"unloaded"}}`)
	w.poll(ctx, time.Now())
	if st, _ = w.Snapshot(); len(st.Tags) != 1 || len(st.PS) != 0 {
		t.Fatalf("unloaded model: installed only: %+v", st)
	}
}

func TestOpenAIEntriesSkipsEmptyIDs(t *testing.T) {
	tags, ps := openAIEntries([]openAIModel{{ID: ""}, {ID: "x"}})
	if len(tags) != 1 || len(ps) != 1 {
		t.Fatalf("entry without id is skipped: %d tags, %d ps", len(tags), len(ps))
	}
	tags, ps = openAIEntries(nil)
	if tags == nil || ps == nil {
		t.Fatal("empty input -> empty lists, never nil (would be sent as null)")
	}
}
