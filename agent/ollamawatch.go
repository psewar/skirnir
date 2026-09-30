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

// Ollama-Zustand vom Agenten (0.14.0; Router poll.apply_ollama_push). Bis 0.13 fragte der Router Ollama alle 5 s durch den
// Tunnel nach /api/tags und /api/ps und erschloss "offline" aus fehlgeschlagenen Abfragen. Jetzt beobachtet der Agent sein
// lokales Ollama selbst und meldet den Zustand als Fakt: installierte Modelle, geladene Modelle, erreichbar ja/nein. Die
// Meldung geht bei jeder Aenderung sofort durch den Tunnel (Rahmen OLLAMA) und spaetestens alle 30 s vollstaendig; solange sie
// frisch ist, pollt der Router diesen Knoten nicht mehr.

// OllamaState ist die Nutzlast des Rahmens OLLAMA. tags/ps sind die Eintraege aus Ollamas Antworten, unveraendert.
type OllamaState struct {
	Up    bool              `json:"up"`
	Error string            `json:"error,omitempty"`
	Rev   uint64            `json:"rev"`
	TS    int64             `json:"ts"`
	Tags  []json.RawMessage `json:"tags"`
	PS    []json.RawMessage `json:"ps"`
}

type OllamaWatcher struct {
	upstream  string
	log       *Logger
	client    *http.Client
	every     time.Duration // Takt fuer /api/ps (geladene Modelle aendern sich mit jeder Anfrage)
	tagsEvery time.Duration // Takt fuer /api/tags (installierte Modelle aendern sich selten)
	downAfter int           // so viele Fehlschlaege in Folge, bis "nicht erreichbar" gemeldet wird

	mu       sync.Mutex
	st       OllamaState
	have     bool // es gibt einen meldbaren Zustand (erster Erfolg oder downAfter Fehlschlaege)
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
	return &OllamaWatcher{upstream: strings.TrimRight(upstream, "/"), log: log, client: &http.Client{Timeout: 3 * time.Second},
		every: time.Second, tagsEvery: 5 * time.Second, downAfter: 3}
}

// Run fragt das lokale Ollama im Takt ab, bis ctx endet.
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

// Snapshot: aktueller Zustand und ob es schon einen meldbaren gibt.
func (w *OllamaWatcher) Snapshot() (OllamaState, bool) {
	if w == nil {
		return OllamaState{}, false
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.st, w.have
}

// get liefert die Liste `models` einer Ollama-Antwort.
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

// psKey: Vergleichsschluessel fuer /api/ps ohne `expires_at` - der Zeitpunkt wandert mit jeder Anfrage weiter, der Router
// braucht ihn nicht, und sonst ginge waehrend jeder Anfrage eine Meldung raus.
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

// poll: eine Abfrage; Revision steigt nur bei einer echten Aenderung (Modelle, Erreichbarkeit).
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
			w.log.Warnf("ollama-watch: Ollama antwortet nicht (%v)", err)
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
			w.log.Infof("ollama-watch: Ollama antwortet wieder")
		}
		w.st.Up, w.st.Error = true, ""
		w.st.Rev++
		w.have = true
	}
}
