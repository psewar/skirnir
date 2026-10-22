package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Ollama state from the agent (0.14.0; Router poll.apply_ollama_push). Until 0.13, the Router polled Ollama every 5 s via the
// tunnel for /api/tags and /api/ps and inferred "offline" from failed requests. Now the agent observes its
// local Ollama itself and reports the state as a fact: installed models, loaded models, reachable yes/no. The
// report goes through the tunnel (frame OLLAMA) immediately on any change and at least every 30 s in full; while it
// is fresh, the Router no longer polls this node.

// OllamaState is the payload of the OLLAMA frame. tags/ps are the entries from Ollama's responses, unchanged.
type OllamaState struct {
	Up    bool              `json:"up"`
	Error string            `json:"error,omitempty"`
	Rev   uint64            `json:"rev"`
	TS    int64             `json:"ts"`
	Tags  []json.RawMessage `json:"tags"`
	PS    []json.RawMessage `json:"ps"`
}

// unsetKey: comparison key before the first response; psKey/rawKey never produce it (empty or lines with trailing newline).
const unsetKey = "\x00unset"

type OllamaWatcher struct {
	upstream  string
	log       *Logger
	client    *http.Client
	every     time.Duration // Interval for /api/ps (loaded models change with every request)
	tagsEvery time.Duration // Interval for /api/tags (installed models rarely change)
	downAfter int           // number of consecutive failures before reporting "unreachable"

	mu       sync.Mutex
	st       OllamaState
	have     bool // there is a reportable state (first success or downAfter failures)
	haveTags bool
	lastTags time.Time
	fails    int
	psKey    string
	tagsKey  string
}

func newOllamaWatcher(upstream string, log *Logger) *OllamaWatcher {
	if upstream == "" {
		return nil
	}
	// Empty lists instead of nil (otherwise `null` is sent) and comparison keys that no real response yields: the first response
	// always counts as a change, even if nothing is loaded. 0.14.0 sent `ps: null` for empty /api/ps, the Router
	// discarded the report and left the node offline (2026-09-30).
	return &OllamaWatcher{upstream: strings.TrimRight(upstream, "/"), log: log, client: &http.Client{Timeout: 3 * time.Second},
		every: time.Second, tagsEvery: 5 * time.Second, downAfter: 3, psKey: unsetKey, tagsKey: unsetKey,
		st: OllamaState{Tags: []json.RawMessage{}, PS: []json.RawMessage{}}}
}

// Run polls the local Ollama at the interval until ctx ends.
func (w *OllamaWatcher) Run(ctx context.Context) {
	for {
		w.poll(ctx, time.Now())
		select {
		case <-ctx.Done():
			return
		case <-time.After(w.every):
		}
	}
}

// Snapshot: current state and whether a reportable one exists yet.
func (w *OllamaWatcher) Snapshot() (OllamaState, bool) {
	if w == nil {
		return OllamaState{}, false
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.st, w.have
}

// get returns the `models` list from an Ollama response.
func (w *OllamaWatcher) get(ctx context.Context, path string) ([]json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, w.upstream+path, nil)
	if err != nil {
		return nil, err
	}
	resp, err := w.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 200))
		return nil, fmt.Errorf("%s: HTTP %d %s", path, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var v struct {
		Models []json.RawMessage `json:"models"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&v); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if v.Models == nil {
		v.Models = []json.RawMessage{}
	}
	return v.Models, nil
}

// psKey: comparison key for /api/ps without `expires_at` - the timestamp shifts with every request, the Router
// doesn't need it, and otherwise a report would go out during every request.
func psKey(models []json.RawMessage) string {
	var b strings.Builder
	for _, m := range models {
		var e map[string]any
		if json.Unmarshal(m, &e) == nil {
			delete(e, "expires_at")
			k, _ := json.Marshal(e) // Map-Schluessel sortiert: stabil
			b.Write(k)
		} else {
			b.Write(m)
		}
		b.WriteByte('\n')
	}
	return b.String()
}

func rawKey(models []json.RawMessage) string {
	var b strings.Builder
	for _, m := range models {
		b.Write(m)
		b.WriteByte('\n')
	}
	return b.String()
}

// poll: one request; Revision increases only on a real change (models, reachability).
func (w *OllamaWatcher) poll(ctx context.Context, now time.Time) {
	ps, err := w.get(ctx, "/api/ps")
	w.mu.Lock()
	needTags := !w.haveTags || now.Sub(w.lastTags) >= w.tagsEvery
	w.mu.Unlock()
	var tags []json.RawMessage
	if err == nil && needTags {
		tags, err = w.get(ctx, "/api/tags")
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if err != nil {
		w.fails++
		if w.fails >= w.downAfter && (w.st.Up || !w.have) {
			w.st.Up, w.st.Error = false, err.Error()
			w.st.Rev++
			w.have = true
			w.log.Warnf("ollama-watch: Ollama not answering (%v)", err)
		}
		return
	}
	w.fails = 0
	changed := !w.st.Up || !w.have
	if k := psKey(ps); k != w.psKey {
		w.psKey, w.st.PS, changed = k, ps, true
	}
	if needTags {
		w.lastTags, w.haveTags = now, true
		if k := rawKey(tags); k != w.tagsKey {
			w.tagsKey, w.st.Tags, changed = k, tags, true
		}
	}
	if changed {
		if !w.st.Up && w.have {
			w.log.Infof("ollama-watch: Ollama answering again")
		}
		w.st.Up, w.st.Error = true, ""
		w.st.Rev++
		w.have = true
	}
}
