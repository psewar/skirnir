package main

import (
	"regexp"
	"strconv"
	"sync"
	"time"
)

// Truncation guard: Ollama silently truncates overly long prompts - the client receives a normal response, but part of the
// history is missing. The only trace is Ollama's own log. Observed 2026-09-18..24 on a desktop node: 7x context
// full ("n_tokens = 65535, truncated = 1"), 1x input halved from 65536 to 32770 tokens. The guard reads the
// lines the supervisor writes to the child log anyway, counts both types, and reports each immediately via MQTT.

const (
	truncationInput       = "input_truncated" // Ollama truncated the prompt before processing
	truncationContextFull = "context_full"    // the context window filled up during processing
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
	Kind        string    `json:"kind"`                   // child process whose log contained the line
	Limit       int       `json:"limit,omitempty"`        // input_truncated: erlaubte Laenge
	Prompt      int       `json:"prompt,omitempty"`       // input_truncated: angelieferte Laenge
	TruncatedTo int       `json:"truncated_to,omitempty"` // input_truncated: length after truncation
	NTokens     int       `json:"n_tokens,omitempty"`
}

type Truncations struct {
	mu     sync.Mutex
	counts map[string]int
	last   *Truncation
	fresh  chan Truncation // buffered; full = message dropped, the counter is still correct
	nowFn  func() time.Time
}

func newTruncations() *Truncations {
	return &Truncations{counts: map[string]int{}, fresh: make(chan Truncation, 32), nowFn: time.Now}
}

func atoi(s string) int { n, _ := strconv.Atoi(s); return n }

// check evaluates a log line. Returns true if it was a truncation.
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

// status returns the counters since service start and the last truncation (nil = none).
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
