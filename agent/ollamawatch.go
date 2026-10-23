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
	// UpstreamState (OpenAI-compatible upstream only, agent 0.22.0): "held" = stopped on the router's order to free the GPU,
	// "starting" = started again (or at agent start) and not answering yet. Both come with up=true and nothing loaded:
	// the node is there, its model is not. Empty = the server answers normally.
	UpstreamState string `json:"upstream_state,omitempty"`
}

// Values of OllamaState.UpstreamState.
const (
	upstreamHeld     = "held"
	upstreamStarting = "starting"
)

// upstreamStartGrace: how long a starting OpenAI-compatible server may not answer before it counts as down. A large model
// takes about a minute to load (Strata, 2026-10-23: 25-60 s).
const upstreamStartGrace = 3 * time.Minute

// unsetKey: comparison key before the first response; psKey/rawKey never produce it (empty or lines with trailing newline).
const unsetKey = "\x00unset"

type OllamaWatcher struct {
	upstream  string
	api       string // upstreamOllama or upstreamOpenAI (tunnel.upstream_api)
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
	held     bool      // upstream stopped on the router's order (SetHeld): no polling, state "held"
	startEnd time.Time // until then a non-answering OpenAI upstream is "starting", not down
}

func newOllamaWatcher(upstream, api string, log *Logger) *OllamaWatcher {
	if upstream == "" {
		return nil
	}
	// Empty lists instead of nil (otherwise `null` is sent) and comparison keys that no real response yields: the first response
	// always counts as a change, even if nothing is loaded. 0.14.0 sent `ps: null` for empty /api/ps, the Router
	// discarded the report and left the node offline (2026-09-30).
	w := &OllamaWatcher{upstream: strings.TrimRight(upstream, "/"), api: api, log: log, client: &http.Client{Timeout: 3 * time.Second},
		every: time.Second, tagsEvery: 5 * time.Second, downAfter: 3, psKey: unsetKey, tagsKey: unsetKey,
		st: OllamaState{Tags: []json.RawMessage{}, PS: []json.RawMessage{}}}
	if api == upstreamOpenAI {
		// An OpenAI-compatible server keeps its model loaded for as long as it runs, so its list changes rarely. A slower
		// poll with a longer timeout keeps a server that is busy reading a long prompt from being reported unreachable.
		w.every, w.client.Timeout = 5*time.Second, 10*time.Second
		w.startEnd = time.Now().Add(upstreamStartGrace) // it starts with the agent and needs a while to load
	}
	return w
}

// SetHeld records that the OpenAI-compatible upstream was stopped (held) or started again (released) on the router's
// order (upstream_hold.go). Held: reported at once as up with nothing loaded, state "held", and not polled. Released:
// "starting" until the server answers, for at most upstreamStartGrace.
func (w *OllamaWatcher) SetHeld(held bool, now time.Time) {
	if w == nil {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.held = held
	w.fails = 0
	if held {
		w.startEnd = time.Time{}
		w.setUpstreamState(upstreamHeld)
		return
	}
	w.startEnd = now.Add(upstreamStartGrace)
	w.setUpstreamState(upstreamStarting)
}

// setUpstreamState: up with nothing loaded and the given state; a new revision only on a change. Caller holds w.mu.
func (w *OllamaWatcher) setUpstreamState(s string) {
	if w.have && w.st.Up && w.st.UpstreamState == s && len(w.st.PS) == 0 {
		return
	}
	w.st.Up, w.st.Error, w.st.UpstreamState = true, "", s
	w.st.PS, w.psKey = []json.RawMessage{}, unsetKey // the next real answer counts as a change
	w.st.Rev++
	w.have = true
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

// getJSON decodes the JSON answer of a GET on the upstream into v.
func (w *OllamaWatcher) getJSON(ctx context.Context, path string, v any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, w.upstream+path, nil)
	if err != nil {
		return err
	}
	resp, err := w.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 200))
		return fmt.Errorf("%s: HTTP %d %s", path, resp.StatusCode, strings.TrimSpace(string(b)))
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(v); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// get returns the `models` list from an Ollama response.
func (w *OllamaWatcher) get(ctx context.Context, path string) ([]json.RawMessage, error) {
	var v struct {
		Models []json.RawMessage `json:"models"`
	}
	if err := w.getJSON(ctx, path, &v); err != nil {
		return nil, err
	}
	if v.Models == nil {
		v.Models = []json.RawMessage{}
	}
	return v.Models, nil
}

// openAIModel is one entry of an OpenAI-compatible /v1/models answer. `status` and `meta.n_ctx` are extras some servers
// add (Strata: {"value": "loaded"} and the context length); a plain OpenAI server leaves them out.
type openAIModel struct {
	ID     string `json:"id"`
	Status *struct {
		Value string `json:"value"`
	} `json:"status"`
	Meta struct {
		NCtx int `json:"n_ctx"`
	} `json:"meta"`
}

// openAIEntries turns the models of an OpenAI-compatible server into the Ollama-shaped tags/ps entries the router reads
// (poll.apply_ollama_state). Sizes stay 0 on purpose: the server's GPU memory is counted once, as this agent's child
// (children_vram_mib), and a size of 0 keeps the router's busy unload (/api/generate keep_alive 0) away from a server that
// has no such call. A model without a status counts as loaded, because the server answers for it.
func openAIEntries(models []openAIModel) (tags, ps []json.RawMessage) {
	tags, ps = []json.RawMessage{}, []json.RawMessage{}
	for _, m := range models {
		if m.ID == "" {
			continue
		}
		digest := "openai:" + m.ID
		t, _ := json.Marshal(map[string]any{"name": m.ID, "model": m.ID, "size": 0, "digest": digest,
			"details": map[string]any{"format": "openai"}})
		tags = append(tags, t)
		if m.Status != nil && m.Status.Value != "loaded" {
			continue
		}
		e := map[string]any{"name": m.ID, "model": m.ID, "size": 0, "size_vram": 0, "digest": digest}
		if m.Meta.NCtx > 0 {
			e["context_length"] = m.Meta.NCtx
		}
		p, _ := json.Marshal(e)
		ps = append(ps, p)
	}
	return tags, ps
}

// fetch returns the loaded (ps) and, with needTags, the installed (tags) models. An OpenAI-compatible server has one list
// for both (/v1/models): it serves what it has loaded.
func (w *OllamaWatcher) fetch(ctx context.Context, needTags bool) (ps, tags []json.RawMessage, err error) {
	if w.api == upstreamOpenAI {
		var v struct {
			Data []openAIModel `json:"data"`
		}
		if err := w.getJSON(ctx, "/v1/models", &v); err != nil {
			return nil, nil, err
		}
		tags, ps = openAIEntries(v.Data)
		return ps, tags, nil
	}
	ps, err = w.get(ctx, "/api/ps")
	if err == nil && needTags {
		tags, err = w.get(ctx, "/api/tags")
	}
	return ps, tags, err
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
	w.mu.Lock()
	if w.held { // stopped on purpose (SetHeld): the state is already reported, there is nothing to ask
		w.mu.Unlock()
		return
	}
	// OpenAI-compatible: the one list carries both, so the installed models are taken on every poll.
	needTags := !w.haveTags || now.Sub(w.lastTags) >= w.tagsEvery || w.api == upstreamOpenAI
	w.mu.Unlock()
	ps, tags, err := w.fetch(ctx, needTags)
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.held { // held while the request ran: keep "held"
		return
	}
	if err != nil && now.Before(w.startEnd) {
		w.setUpstreamState(upstreamStarting) // a starting server does not answer yet; that is not "down"
		return
	}
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
	changed := !w.st.Up || !w.have || w.st.UpstreamState != ""
	w.st.UpstreamState, w.startEnd = "", time.Time{} // answering: neither held nor starting any more
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
