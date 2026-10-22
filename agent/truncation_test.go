package main

import (
	"bufio"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Lines verbatim from C:\ProgramData\skirnir-agent\logs\ollama*.log (2026-09-21..24), including the
// timestamp prepended by the childWriter.
const (
	lineInput       = `2026-09-23 21:33:12 time=2026-09-23T21:33:12.738+02:00 level=WARN source=llama_server.go:317 msg="truncating input prompt" limit=32770 prompt=65536 keep=4 new=32770`
	lineContextFull = `2026-09-23 21:32:50 slot      release: id  0 | task 202197 | stop processing: n_tokens = 65535, truncated = 1`
	lineNormal      = `2026-09-24 17:24:46 slot      release: id  0 | task 31907 | stop processing: n_tokens = 76079, truncated = 0`
)

func TestTruncationInputDetected(t *testing.T) {
	k := newTruncations()
	fixedNow := time.Date(2026, 9, 23, 21, 33, 12, 0, time.Local)
	k.nowFn = func() time.Time { return fixedNow }
	if !k.inspect("ollama", lineInput) {
		t.Fatal("Eingabekuerzung nicht erkannt")
	}
	a, l := k.Snapshot()
	if a[truncationInput] != 1 || a[truncationContextFull] != 0 {
		t.Fatalf("Zaehler falsch: %v", a)
	}
	if l == nil || l.Limit != 32770 || l.Prompt != 65536 || l.TruncatedTo != 32770 || l.Kind != "ollama" || !l.At.Equal(fixedNow) {
		t.Fatalf("Details falsch: %+v", l)
	}
	select {
	case e := <-k.fresh:
		if e.Type != truncationInput {
			t.Fatalf("Meldung mit falscher Art: %+v", e)
		}
	default:
		t.Fatal("keine Sofortmeldung")
	}
}

func TestTruncationContextFullDetected(t *testing.T) {
	k := newTruncations()
	if !k.inspect("ollama", lineContextFull) {
		t.Fatal("volles Kontextfenster nicht erkannt")
	}
	a, l := k.Snapshot()
	if a[truncationContextFull] != 1 || l.NTokens != 65535 {
		t.Fatalf("falsch: %v %+v", a, l)
	}
}

// The normal line differs only in "truncated = 0" - exactly that one must not count, otherwise the
// agent reports every request (over 5000 such lines since 2026-09-18).
func TestNormalLineDoesNotCount(t *testing.T) {
	k := newTruncations()
	for _, z := range []string{lineNormal, "", "truncated = 1", `msg="truncating input prompt"`} {
		if k.inspect("ollama", z) {
			t.Fatalf("Fehlalarm bei %q", z)
		}
	}
	if a, l := k.Snapshot(); a[truncationInput]+a[truncationContextFull] != 0 || l != nil {
		t.Fatalf("Zaehler bewegt: %v %+v", a, l)
	}
}

// A full buffer must never block the log writer - the counter keeps counting.
func TestFullBufferDoesNotBlock(t *testing.T) {
	k := newTruncations()
	finished := make(chan struct{})
	go func() {
		for i := 0; i < 100; i++ {
			k.inspect("ollama", lineContextFull)
		}
		close(finished)
	}()
	select {
	case <-finished:
	case <-time.After(2 * time.Second):
		t.Fatal("pruefe blockiert bei vollem Puffer")
	}
	if a, _ := k.Snapshot(); a[truncationContextFull] != 100 {
		t.Fatalf("Zaehler %d statt 100", a[truncationContextFull])
	}
}

// The childWriter forwards every complete line, even across Write boundaries.
func TestChildWriterPassesLinesOn(t *testing.T) {
	k := newTruncations()
	w := newChildWriter(LogCfg{Dir: t.TempDir()}, "ollama")
	t.Cleanup(func() { w.lj.Close() })
	w.onLine = func(z string) { k.inspect("ollama", z) }
	part := `slot release: id 0 | task 1 | stop processing: n_tokens = 65535, trunc`
	w.Write([]byte(part))
	w.Write([]byte("ated = 1\r\nnaechste Zeile\n"))
	if a, _ := k.Snapshot(); a[truncationContextFull] != 1 {
		t.Fatalf("ueber zwei Writes verteilte Zeile nicht erkannt: %v", a)
	}
}

// Counter-test on real logs (only if SKIRNIR_OLLAMA_LOGS points to a folder with ollama*.log):
// counts what the watcher would have detected in the existing files.
func TestRealOllamaLogs(t *testing.T) {
	dir := os.Getenv("SKIRNIR_OLLAMA_LOGS")
	if dir == "" {
		t.Skip("SKIRNIR_OLLAMA_LOGS nicht gesetzt")
	}
	logFiles, _ := filepath.Glob(filepath.Join(dir, "ollama*.log"))
	k, logLines := newTruncations(), 0
	for _, p := range logFiles {
		f, err := os.Open(p)
		if err != nil {
			t.Fatal(err)
		}
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1024*1024), 1024*1024)
		for sc.Scan() {
			logLines++
			k.inspect("ollama", sc.Text())
		}
		f.Close()
	}
	a, _ := k.Snapshot()
	t.Logf("%d Dateien, %d Zeilen: %d eingabe_gekuerzt, %d kontext_voll", len(logFiles), logLines, a[truncationInput], a[truncationContextFull])
}
