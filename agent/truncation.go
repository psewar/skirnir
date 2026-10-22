package main

import (
	"regexp"
	"strconv"
	"sync"
	"time"
)

// Kuerzungswaechter: Ollama kuerzt zu lange Prompts still - der Client bekommt eine normale Antwort, nur fehlt ihm
// ein Teil des Verlaufs. Einzige Spur ist Ollamas eigenes Log. Gesehen 2026-09-18..24 auf PSEWAR-2026: 7x Kontext
// voll ("n_tokens = 65535, truncated = 1"), 1x Eingabe von 65536 auf 32770 Token halbiert. Der Waechter liest die
// Zeilen mit, die der Supervisor ohnehin ins Kind-Log schreibt, zaehlt beide Arten und meldet jede sofort per MQTT.

const (
	truncationInput       = "input_truncated" // Ollama hat den Prompt vor der Verarbeitung abgeschnitten
	truncationContextFull = "context_full"    // das Kontextfenster lief waehrend der Verarbeitung voll
)

var (
	// level=WARN source=llama_server.go:317 msg="truncating input prompt" limit=32770 prompt=65536 keep=4 new=32770
	reInput = regexp.MustCompile(`msg="truncating input prompt".*?\blimit=(\d+)\s+prompt=(\d+)(?:.*?\bnew=(\d+))?`)
	// slot      release: id  0 | task 202197 | stop processing: n_tokens = 65535, truncated = 1
	reContextFull = regexp.MustCompile(`stop processing: n_tokens = (\d+), truncated = 1\b`)
)

type Truncation struct {
	Type        string    `json:"type"`
	At          time.Time `json:"at"`
	Kind        string    `json:"kind"`                   // Kindprozess, dessen Log die Zeile enthielt
	Limit       int       `json:"limit,omitempty"`        // input_truncated: erlaubte Laenge
	Prompt      int       `json:"prompt,omitempty"`       // input_truncated: angelieferte Laenge
	TruncatedTo int       `json:"truncated_to,omitempty"` // input_truncated: Laenge nach dem Kuerzen
	NTokens     int       `json:"n_tokens,omitempty"`
}

type Truncations struct {
	mu     sync.Mutex
	counts map[string]int
	last   *Truncation
	fresh  chan Truncation // gepuffert; voll = Meldung verworfen, der Zaehler stimmt trotzdem
	nowFn  func() time.Time
}

func newTruncations() *Truncations {
	return &Truncations{counts: map[string]int{}, fresh: make(chan Truncation, 32), nowFn: time.Now}
}

func atoi(s string) int { n, _ := strconv.Atoi(s); return n }

// pruefe wertet eine Log-Zeile aus. Liefert true, wenn sie eine Kuerzung war.
func (k *Truncations) inspect(kind, logLine string) bool {
	var e Truncation
	if m := reInput.FindStringSubmatch(logLine); m != nil {
		e = Truncation{Type: truncationInput, Limit: atoi(m[1]), Prompt: atoi(m[2]), TruncatedTo: atoi(m[3])}
	} else if m := reContextFull.FindStringSubmatch(logLine); m != nil {
		e = Truncation{Type: truncationContextFull, NTokens: atoi(m[1])}
	} else {
		return false
	}
	e.Kind, e.At = kind, k.nowFn()
	k.mu.Lock()
	k.counts[e.Type]++
	k.last = &e
	k.mu.Unlock()
	select {
	case k.fresh <- e:
	default:
	}
	return true
}

// Stand liefert die Zaehler seit Dienststart und die letzte Kuerzung (nil = keine).
func (k *Truncations) Snapshot() (map[string]int, *Truncation) {
	k.mu.Lock()
	defer k.mu.Unlock()
	a := map[string]int{truncationInput: k.counts[truncationInput], truncationContextFull: k.counts[truncationContextFull]}
	if k.last == nil {
		return a, nil
	}
	l := *k.last
	return a, &l
}
