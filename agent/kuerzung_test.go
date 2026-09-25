package main

import (
	"bufio"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Zeilen woertlich aus C:\ProgramData\skirnir-agent\logs\ollama*.log (2026-09-21..24), inklusive des
// Zeitstempels, den der childWriter voranstellt.
const (
	zeileEingabe     = `2026-09-23 21:33:12 time=2026-09-23T21:33:12.738+02:00 level=WARN source=llama_server.go:317 msg="truncating input prompt" limit=32770 prompt=65536 keep=4 new=32770`
	zeileKontextVoll = `2026-09-23 21:32:50 slot      release: id  0 | task 202197 | stop processing: n_tokens = 65535, truncated = 1`
	zeileNormal      = `2026-09-24 17:24:46 slot      release: id  0 | task 31907 | stop processing: n_tokens = 76079, truncated = 0`
)

func TestKuerzungEingabeErkannt(t *testing.T) {
	k := newKuerzungen()
	fest := time.Date(2026, 9, 23, 21, 33, 12, 0, time.Local)
	k.jetztFn = func() time.Time { return fest }
	if !k.pruefe("ollama", zeileEingabe) {
		t.Fatal("Eingabekuerzung nicht erkannt")
	}
	a, l := k.Stand()
	if a[kuerzungEingabe] != 1 || a[kuerzungKontextVoll] != 0 {
		t.Fatalf("Zaehler falsch: %v", a)
	}
	if l == nil || l.Limit != 32770 || l.Prompt != 65536 || l.Neu != 32770 || l.Kind != "ollama" || !l.Zeit.Equal(fest) {
		t.Fatalf("Details falsch: %+v", l)
	}
	select {
	case e := <-k.neu:
		if e.Art != kuerzungEingabe {
			t.Fatalf("Meldung mit falscher Art: %+v", e)
		}
	default:
		t.Fatal("keine Sofortmeldung")
	}
}

func TestKuerzungKontextVollErkannt(t *testing.T) {
	k := newKuerzungen()
	if !k.pruefe("ollama", zeileKontextVoll) {
		t.Fatal("volles Kontextfenster nicht erkannt")
	}
	a, l := k.Stand()
	if a[kuerzungKontextVoll] != 1 || l.NTokens != 65535 {
		t.Fatalf("falsch: %v %+v", a, l)
	}
}

// Die Normalzeile unterscheidet sich nur in "truncated = 0" - genau die darf nicht zaehlen, sonst meldet der
// Agent jede Anfrage (ueber 5000 solcher Zeilen seit 2026-09-18).
func TestNormaleZeileZaehltNicht(t *testing.T) {
	k := newKuerzungen()
	for _, z := range []string{zeileNormal, "", "truncated = 1", `msg="truncating input prompt"`} {
		if k.pruefe("ollama", z) {
			t.Fatalf("Fehlalarm bei %q", z)
		}
	}
	if a, l := k.Stand(); a[kuerzungEingabe]+a[kuerzungKontextVoll] != 0 || l != nil {
		t.Fatalf("Zaehler bewegt: %v %+v", a, l)
	}
}

// Ein voller Puffer darf den Log-Schreiber nie blockieren - der Zaehler zaehlt trotzdem weiter.
func TestVollerPufferBlockiertNicht(t *testing.T) {
	k := newKuerzungen()
	fertig := make(chan struct{})
	go func() {
		for i := 0; i < 100; i++ {
			k.pruefe("ollama", zeileKontextVoll)
		}
		close(fertig)
	}()
	select {
	case <-fertig:
	case <-time.After(2 * time.Second):
		t.Fatal("pruefe blockiert bei vollem Puffer")
	}
	if a, _ := k.Stand(); a[kuerzungKontextVoll] != 100 {
		t.Fatalf("Zaehler %d statt 100", a[kuerzungKontextVoll])
	}
}

// Der childWriter reicht jede vollstaendige Zeile weiter, auch ueber Write-Grenzen hinweg.
func TestChildWriterReichtZeilenWeiter(t *testing.T) {
	k := newKuerzungen()
	w := newChildWriter(LogCfg{Dir: t.TempDir()}, "ollama")
	t.Cleanup(func() { w.lj.Close() })
	w.onLine = func(z string) { k.pruefe("ollama", z) }
	teil := `slot release: id 0 | task 1 | stop processing: n_tokens = 65535, trunc`
	w.Write([]byte(teil))
	w.Write([]byte("ated = 1\r\nnaechste Zeile\n"))
	if a, _ := k.Stand(); a[kuerzungKontextVoll] != 1 {
		t.Fatalf("ueber zwei Writes verteilte Zeile nicht erkannt: %v", a)
	}
}

// Gegenprobe an echten Logs (nur wenn SKIRNIR_OLLAMA_LOGS auf einen Ordner mit ollama*.log zeigt):
// zaehlt, was der Waechter in den vorhandenen Dateien erkannt haette.
func TestEchteOllamaLogs(t *testing.T) {
	dir := os.Getenv("SKIRNIR_OLLAMA_LOGS")
	if dir == "" {
		t.Skip("SKIRNIR_OLLAMA_LOGS nicht gesetzt")
	}
	dateien, _ := filepath.Glob(filepath.Join(dir, "ollama*.log"))
	k, zeilen := newKuerzungen(), 0
	for _, p := range dateien {
		f, err := os.Open(p)
		if err != nil {
			t.Fatal(err)
		}
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1024*1024), 1024*1024)
		for sc.Scan() {
			zeilen++
			k.pruefe("ollama", sc.Text())
		}
		f.Close()
	}
	a, _ := k.Stand()
	t.Logf("%d Dateien, %d Zeilen: %d eingabe_gekuerzt, %d kontext_voll", len(dateien), zeilen, a[kuerzungEingabe], a[kuerzungKontextVoll])
}
