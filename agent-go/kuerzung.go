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
	kuerzungEingabe     = "eingabe_gekuerzt" // Ollama hat den Prompt vor der Verarbeitung abgeschnitten
	kuerzungKontextVoll = "kontext_voll"     // das Kontextfenster lief waehrend der Verarbeitung voll
)

var (
	// level=WARN source=llama_server.go:317 msg="truncating input prompt" limit=32770 prompt=65536 keep=4 new=32770
	reEingabe = regexp.MustCompile(`msg="truncating input prompt".*?\blimit=(\d+)\s+prompt=(\d+)(?:.*?\bnew=(\d+))?`)
	// slot      release: id  0 | task 202197 | stop processing: n_tokens = 65535, truncated = 1
	reKontextVoll = regexp.MustCompile(`stop processing: n_tokens = (\d+), truncated = 1\b`)
)

type Kuerzung struct {
	Art     string    `json:"art"`
	Zeit    time.Time `json:"zeit"`
	Kind    string    `json:"kind"`             // Kindprozess, dessen Log die Zeile enthielt
	Limit   int       `json:"limit,omitempty"`  // eingabe_gekuerzt: erlaubte Laenge
	Prompt  int       `json:"prompt,omitempty"` // eingabe_gekuerzt: angelieferte Laenge
	Neu     int       `json:"neu,omitempty"`    // eingabe_gekuerzt: Laenge nach dem Kuerzen
	NTokens int       `json:"n_tokens,omitempty"`
}

type Kuerzungen struct {
	mu      sync.Mutex
	anzahl  map[string]int
	letzte  *Kuerzung
	neu     chan Kuerzung // gepuffert; voll = Meldung verworfen, der Zaehler stimmt trotzdem
	jetztFn func() time.Time
}

func newKuerzungen() *Kuerzungen {
	return &Kuerzungen{anzahl: map[string]int{}, neu: make(chan Kuerzung, 32), jetztFn: time.Now}
}

func atoi(s string) int { n, _ := strconv.Atoi(s); return n }

// pruefe wertet eine Log-Zeile aus. Liefert true, wenn sie eine Kuerzung war.
func (k *Kuerzungen) pruefe(kind, zeile string) bool {
	var e Kuerzung
	if m := reEingabe.FindStringSubmatch(zeile); m != nil {
		e = Kuerzung{Art: kuerzungEingabe, Limit: atoi(m[1]), Prompt: atoi(m[2]), Neu: atoi(m[3])}
	} else if m := reKontextVoll.FindStringSubmatch(zeile); m != nil {
		e = Kuerzung{Art: kuerzungKontextVoll, NTokens: atoi(m[1])}
	} else {
		return false
	}
	e.Kind, e.Zeit = kind, k.jetztFn()
	k.mu.Lock()
	k.anzahl[e.Art]++
	k.letzte = &e
	k.mu.Unlock()
	select {
	case k.neu <- e:
	default:
	}
	return true
}

// Stand liefert die Zaehler seit Dienststart und die letzte Kuerzung (nil = keine).
func (k *Kuerzungen) Stand() (map[string]int, *Kuerzung) {
	k.mu.Lock()
	defer k.mu.Unlock()
	a := map[string]int{kuerzungEingabe: k.anzahl[kuerzungEingabe], kuerzungKontextVoll: k.anzahl[kuerzungKontextVoll]}
	if k.letzte == nil {
		return a, nil
	}
	l := *k.letzte
	return a, &l
}
