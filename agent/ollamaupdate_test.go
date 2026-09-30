package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func quietLogger() *Logger { return &Logger{Logger: log.New(io.Discard, "", 0)} }

func TestParseChecksum(t *testing.T) {
	txt := "aaaa  ./install.sh\n" + strings.Repeat("b", 64) + "  ./ollama-windows-amd64.zip\n" + strings.Repeat("c", 64) + " *ollama-linux-amd64.tar.zst\n"
	if s, err := parseChecksum(txt, "ollama-windows-amd64.zip"); err != nil || s != strings.Repeat("b", 64) {
		t.Fatalf("zip: %q %v", s, err)
	}
	if s, err := parseChecksum(txt, "ollama-linux-amd64.tar.zst"); err != nil || s != strings.Repeat("c", 64) {
		t.Fatalf("tar (Sternchen-Format): %q %v", s, err)
	}
	if _, err := parseChecksum(txt, "OllamaSetup.exe"); err == nil {
		t.Fatal("fehlender Eintrag muss scheitern")
	}
}

func TestParseOllamaVersion(t *testing.T) {
	cases := map[string]string{
		"ollama version is 0.34.4\n": "0.34.4",
		"Warning: could not connect to a running Ollama instance\nWarning: client version is 0.34.4\n": "0.34.4",
		"Warning: client version is 0.34.4\nollama version is 0.34.0\n":                                "0.34.4",
	}
	for in, want := range cases {
		if got, err := parseOllamaVersion(in); err != nil || got != want {
			t.Errorf("%q -> %q %v, erwartet %q", in, got, err, want)
		}
	}
	if _, err := parseOllamaVersion("nichts"); err == nil {
		t.Fatal("ohne Version muss scheitern")
	}
}

func TestOllamaVerifyOrder(t *testing.T) {
	sum := strings.Repeat("a", 64)
	// kein Ollama-Kind: Auftrag abgelehnt, egal was drinsteht
	u := newOllamaUpdater(OllamaUpdateCfg{}, []ChildSpec{{Name: "stt", Cmd: "/x/python"}}, nil, "", quietLogger())
	if err := u.verifyOrder(OllamaUpdateOrder{Version: "0.34.4", File: "ollama-windows-amd64.zip", Sha256: sum}); err == nil || !strings.Contains(err.Error(), "kein Kind") {
		t.Fatalf("ohne Ollama-Kind: %v", err)
	}
	off := false
	u = newOllamaUpdater(OllamaUpdateCfg{Enabled: &off}, []ChildSpec{{Name: "ollama", Cmd: `C:\O\ollama.exe`}}, nil, "", quietLogger())
	if err := u.verifyOrder(OllamaUpdateOrder{Version: "0.34.4", File: "ollama-windows-amd64.zip", Sha256: sum}); err == nil || !strings.Contains(err.Error(), "abgewaehlt") {
		t.Fatalf("abgewaehlt: %v", err)
	}
	u = newOllamaUpdater(OllamaUpdateCfg{}, []ChildSpec{{Name: "ollama", Cmd: `C:\O\ollama.exe`}}, nil, "", quietLogger())
	u.archive = func() (string, error) { return "ollama-windows-amd64.zip", nil }
	if u.child != "ollama" || u.dir != filepath.Dir(`C:\O\ollama.exe`) {
		t.Fatalf("Kind nicht erkannt: %q %q", u.child, u.dir)
	}
	for _, o := range []OllamaUpdateOrder{
		{Version: "0.34.4", File: "OllamaSetup.exe", Sha256: sum},        // falsches Archiv
		{Version: "../x", File: "ollama-windows-amd64.zip", Sha256: sum}, // Version als Pfad
		{Version: "0.34.4", File: "ollama-windows-amd64.zip"},            // ohne Hash
	} {
		if err := u.verifyOrder(o); err == nil {
			t.Errorf("Auftrag %+v haette scheitern muessen", o)
		}
	}
	if err := u.verifyOrder(OllamaUpdateOrder{Version: "0.34.4", File: "ollama-windows-amd64.zip", Sha256: sum}); err != nil {
		t.Fatalf("gueltiger Auftrag: %v", err)
	}
}

func writeFile(t *testing.T, p, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func readFile(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		return "<" + err.Error() + ">"
	}
	return string(b)
}

func TestOllamaSwapAndRollback(t *testing.T) {
	root := t.TempDir()
	dir, newDir, backup := filepath.Join(root, "Ollama"), filepath.Join(root, "new"), filepath.Join(root, "old")
	writeFile(t, filepath.Join(dir, "ollama.exe"), "alt")
	writeFile(t, filepath.Join(dir, "lib", "ollama", "a.dll"), "alt")
	writeFile(t, filepath.Join(dir, "ollama app.exe"), "tray") // bleibt unberuehrt
	writeFile(t, filepath.Join(newDir, "ollama.exe"), "neu")
	writeFile(t, filepath.Join(newDir, "lib", "ollama", "b.dll"), "neu")
	os.MkdirAll(backup, 0o755)
	moved, err := swapTop(dir, newDir, backup)
	if err != nil || len(moved) != 2 {
		t.Fatalf("swap: %v %v", moved, err)
	}
	if readFile(t, filepath.Join(dir, "ollama.exe")) != "neu" || readFile(t, filepath.Join(dir, "lib", "ollama", "b.dll")) != "neu" ||
		readFile(t, filepath.Join(dir, "ollama app.exe")) != "tray" || readFile(t, filepath.Join(backup, "ollama.exe")) != "alt" {
		t.Fatal("nach dem Tausch stimmen die Dateien nicht")
	}
	if _, err := os.Stat(filepath.Join(dir, "lib", "ollama", "a.dll")); err == nil {
		t.Fatal("alte lib haette in die Sicherung wandern muessen")
	}
	if err := rollbackTop(dir, newDir, backup, moved); err != nil {
		t.Fatalf("rollback: %v", err)
	}
	if readFile(t, filepath.Join(dir, "ollama.exe")) != "alt" || readFile(t, filepath.Join(dir, "lib", "ollama", "a.dll")) != "alt" ||
		readFile(t, filepath.Join(newDir, "ollama.exe")) != "neu" {
		t.Fatal("nach dem Rollback stimmen die Dateien nicht")
	}
}

func TestExtractZipSlip(t *testing.T) {
	root := t.TempDir()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	f, _ := w.Create("../evil.txt")
	f.Write([]byte("x"))
	w.Close()
	zp := filepath.Join(root, "a.zip")
	os.WriteFile(zp, buf.Bytes(), 0o644)
	if err := extractZip(zp, filepath.Join(root, "out")); err == nil || !strings.Contains(err.Error(), "unzulaessig") {
		t.Fatalf("Zip-Slip nicht erkannt: %v", err)
	}
}

// releaseServer spielt die Release-Quelle: /v<ver>/sha256sum.txt und /v<ver>/<archiv> mit ollama.exe + lib/ollama/x.dll.
func releaseServer(t *testing.T, ver, archive string) (*httptest.Server, string, int64) {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	f, _ := w.Create("ollama.exe")
	f.Write([]byte("neu " + ver))
	f, _ = w.Create("lib/ollama/x.dll")
	f.Write([]byte("neu"))
	w.Close()
	data := buf.Bytes()
	h := sha256.Sum256(data)
	sum := hex.EncodeToString(h[:])
	mux := http.NewServeMux()
	mux.HandleFunc("/v"+ver+"/sha256sum.txt", func(rw http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(rw, "%s  ./install.sh\n%s  ./%s\n", strings.Repeat("0", 64), sum, archive)
	})
	mux.HandleFunc("/v"+ver+"/"+archive, func(rw http.ResponseWriter, r *http.Request) { rw.Write(data) })
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, sum, int64(len(data))
}

func testUpdater(t *testing.T, srv *httptest.Server, dir string) *OllamaUpdater {
	t.Helper()
	exe := filepath.Join(dir, "ollama.exe")
	u := newOllamaUpdater(OllamaUpdateCfg{Source: srv.URL + "/"}, []ChildSpec{{Name: "ollama", Cmd: exe}}, nil, "", quietLogger())
	u.archive = func() (string, error) { return "ollama-windows-amd64.zip", nil }
	u.free = func(string) (uint64, error) { return 1 << 40, nil }
	u.probe = func(p string) (string, error) { return strings.TrimPrefix(readFile(t, p), "neu "), nil }
	u.verifyWait, u.verifyPoll = 2*time.Second, 100*time.Millisecond
	u.swapWait = 5 * time.Second
	return u
}

// runMitFreigabe spielt den Router: sobald der Auftrag "ready" meldet, kommt die Freigabe. Liefert den Fehler von run und
// ob "ready" vor dem Tausch gemeldet wurde, waehrend die alten Dateien noch an ihrem Platz lagen.
func runMitFreigabe(t *testing.T, u *OllamaUpdater, o OllamaUpdateOrder, dir string) (error, bool) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- u.run(o) }()
	readyVorTausch := false
	for {
		select {
		case err := <-done:
			return err, readyVorTausch
		case <-time.After(20 * time.Millisecond):
			if r := u.Report(); r != nil && r.State == "ready" && !readyVorTausch {
				readyVorTausch = readFile(t, filepath.Join(dir, "ollama.exe")) == "alt"
				u.Swap(o.Version)
			}
		}
	}
}

func TestOllamaUpdateRun(t *testing.T) {
	const ver, archive = "0.34.4", "ollama-windows-amd64.zip"
	srv, sum, size := releaseServer(t, ver, archive)
	root := t.TempDir()
	dir := filepath.Join(root, "Ollama")
	writeFile(t, filepath.Join(dir, "ollama.exe"), "alt")
	writeFile(t, filepath.Join(dir, "lib", "ollama", "a.dll"), "alt")
	u := testUpdater(t, srv, dir)
	// "laufendes Ollama" meldet, was gerade als ollama.exe im Ordner liegt
	u.live = func(context.Context) string {
		return strings.TrimPrefix(readFile(t, filepath.Join(dir, "ollama.exe")), "neu ")
	}
	err, ready := runMitFreigabe(t, u, OllamaUpdateOrder{Version: ver, File: archive, Sha256: sum, Size: size}, dir)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	if !ready {
		t.Fatal("\"ready\" muss vor dem Tausch kommen, solange die alten Dateien noch liegen")
	}
	if readFile(t, filepath.Join(dir, "ollama.exe")) != "neu "+ver || readFile(t, filepath.Join(dir, "lib", "ollama", "x.dll")) != "neu" {
		t.Fatal("neue Dateien nicht eingesetzt")
	}
	if _, err := os.Stat(filepath.Join(dir, "lib", "ollama", "a.dll")); err == nil {
		t.Fatal("alte lib muss weg sein")
	}
	if _, err := os.Stat(filepath.Join(root, ".skirnir-ollama-update")); err == nil {
		t.Fatal("Staging-Ordner muss nach dem Erfolg weg sein")
	}
	if r := u.Report(); r == nil || r.State != "applied" {
		t.Fatalf("Bericht: %+v", r)
	}
	// Hash im Auftrag passt nicht zu sha256sum.txt der Quelle -> abgelehnt, nichts angefasst
	err = u.run(OllamaUpdateOrder{Version: ver, File: archive, Sha256: strings.Repeat("f", 64), Size: size})
	if err == nil || !strings.Contains(err.Error(), "widerspricht") {
		t.Fatalf("falscher Hash: %v", err)
	}
}

func TestOllamaUpdateRollback(t *testing.T) {
	const ver, archive = "0.34.4", "ollama-windows-amd64.zip"
	srv, sum, size := releaseServer(t, ver, archive)
	root := t.TempDir()
	dir := filepath.Join(root, "Ollama")
	writeFile(t, filepath.Join(dir, "ollama.exe"), "alt")
	writeFile(t, filepath.Join(dir, "lib", "ollama", "a.dll"), "alt")
	u := testUpdater(t, srv, dir)
	u.live = func(context.Context) string { return "0.34.0" } // das neue Ollama meldet sich nie
	err, _ := runMitFreigabe(t, u, OllamaUpdateOrder{Version: ver, File: archive, Sha256: sum, Size: size}, dir)
	if err == nil || !strings.Contains(err.Error(), "zurueckgesetzt (true)") {
		t.Fatalf("Rollback erwartet: %v", err)
	}
	if readFile(t, filepath.Join(dir, "ollama.exe")) != "alt" || readFile(t, filepath.Join(dir, "lib", "ollama", "a.dll")) != "alt" {
		t.Fatal("Sicherung nicht zurueckgespielt")
	}
	if _, err := os.Stat(filepath.Join(dir, "lib", "ollama", "x.dll")); err == nil {
		t.Fatal("neue lib haette verschwinden muessen")
	}
}

// Ohne Freigabe des Routers wird nichts getauscht: Ollama laeuft unveraendert weiter, der Staging-Ordner ist weg.
func TestOllamaUpdateOhneFreigabe(t *testing.T) {
	const ver, archive = "0.34.4", "ollama-windows-amd64.zip"
	srv, sum, size := releaseServer(t, ver, archive)
	root := t.TempDir()
	dir := filepath.Join(root, "Ollama")
	writeFile(t, filepath.Join(dir, "ollama.exe"), "alt")
	u := testUpdater(t, srv, dir)
	u.swapWait = 300 * time.Millisecond
	u.Swap("0.0.1") // alte Freigabe eines frueheren Auftrags: wird zu Beginn verworfen
	err := u.run(OllamaUpdateOrder{Version: ver, File: archive, Sha256: sum, Size: size})
	if err == nil || !strings.Contains(err.Error(), "nicht innerhalb") {
		t.Fatalf("Zeitueberschreitung erwartet: %v", err)
	}
	if readFile(t, filepath.Join(dir, "ollama.exe")) != "alt" {
		t.Fatal("ohne Freigabe darf nichts getauscht werden")
	}
	if _, err := os.Stat(filepath.Join(root, ".skirnir-ollama-update")); err == nil {
		t.Fatal("Staging-Ordner muss auch nach der Zeitueberschreitung weg sein")
	}
	// Freigabe fuer eine andere Version zaehlt nicht
	u.swapWait = 300 * time.Millisecond
	go func() {
		for u.Report() == nil || u.Report().State != "ready" {
			time.Sleep(10 * time.Millisecond)
		}
		u.Swap("9.9.9")
	}()
	if err := u.run(OllamaUpdateOrder{Version: ver, File: archive, Sha256: sum, Size: size}); err == nil {
		t.Fatal("Freigabe fuer eine andere Version darf nicht tauschen")
	}
	if readFile(t, filepath.Join(dir, "ollama.exe")) != "alt" {
		t.Fatal("falsche Version freigegeben: trotzdem getauscht")
	}
}
