package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// signedOrder baut Manifest + Signatur wie deploy.py --agent (kompaktes JSON, sortierte Schluessel).
func signedOrder(t *testing.T, priv ed25519.PrivateKey, ver, file, sum string) UpdateOrder {
	t.Helper()
	m := map[string]any{"version": ver, "generated": "2026-09-25T10:00:00",
		"files": []map[string]any{{"arch": runtime.GOARCH, "name": file, "os": runtime.GOOS, "sha256": sum, "size": 3}}}
	b, _ := json.Marshal(m) // Go sortiert Map-Schluessel: entspricht sort_keys + separators ohne Leerzeichen
	return UpdateOrder{Version: ver, File: file, URL: "http://127.0.0.1:1/x", Sha256: sum, Size: 3, Token: "t",
		Manifest: string(b), Signature: base64.StdEncoding.EncodeToString(ed25519.Sign(priv, b))}
}

func TestUpdaterVerifyOrder(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	sum := hex.EncodeToString(sha256.New().Sum(nil))
	u := newUpdater(UpdateCfg{PublicKey: base64.StdEncoding.EncodeToString(pub)}, "", nil)
	o := signedOrder(t, priv, "9.9.9", "skirnir-agent-9.9.9-test", sum)
	f, err := u.verifyOrder(o)
	if err != nil || f.Name != o.File {
		t.Fatalf("gueltiger Auftrag abgelehnt: %v", err)
	}
	// Signatur von einem anderen Schluessel
	_, priv2, _ := ed25519.GenerateKey(rand.Reader)
	if _, err := u.verifyOrder(signedOrder(t, priv2, "9.9.9", o.File, sum)); err == nil || !strings.Contains(err.Error(), "Signatur") {
		t.Fatalf("fremde Signatur akzeptiert: %v", err)
	}
	// Manifest nachtraeglich veraendert
	bad := o
	bad.Manifest = strings.Replace(o.Manifest, "9.9.9", "9.9.8", 1)
	if _, err := u.verifyOrder(bad); err == nil {
		t.Fatal("veraendertes Manifest akzeptiert")
	}
	// Hash im Auftrag widerspricht dem Manifest
	bad = o
	bad.Sha256 = strings.Repeat("0", 64)
	if _, err := u.verifyOrder(bad); err == nil || !strings.Contains(err.Error(), "SHA-256") {
		t.Fatalf("Hash-Widerspruch akzeptiert: %v", err)
	}
	// Datei nicht fuer dieses OS im Manifest
	bad = o
	bad.File = "andere-datei"
	if _, err := u.verifyOrder(bad); err == nil {
		t.Fatal("fremde Datei akzeptiert")
	}
	// ohne Schluessel: nie
	u2 := newUpdater(UpdateCfg{}, "", nil)
	if _, err := u2.verifyOrder(o); err == nil || !strings.Contains(err.Error(), "public_key") {
		t.Fatalf("ohne public_key akzeptiert: %v", err)
	}
	// abgewaehlt
	off := false
	u3 := newUpdater(UpdateCfg{Enabled: &off, PublicKey: base64.StdEncoding.EncodeToString(pub)}, "", nil)
	if _, err := u3.verifyOrder(o); err == nil || !strings.Contains(err.Error(), "abgewaehlt") {
		t.Fatalf("abgewaehlt akzeptiert: %v", err)
	}
}

// Review 2026-09-25: eine gesperrte .old wird beim Tausch als .old-<ts> geparkt; Cleanup kannte nur .old und liess die
// geparkten liegen - und startete das Relay genau dann nicht neu.
func TestUpdaterCleanupRaeumtGeparkteAlteBinaries(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "agent.exe")
	for _, n := range []string{exe + ".old", exe + ".old-1758800000", exe + ".new"} {
		if err := os.WriteFile(n, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if got := oldBinaries(exe); len(got) != 2 {
		t.Fatalf("oldBinaries: %v", got)
	}
	log, _ := newLogger(LogCfg{Dir: t.TempDir()}, "test", false)
	called := false
	if !cleanupOld(exe, func() { called = true }, time.Millisecond, log) || !called {
		t.Fatalf("cleanupOld: nicht alles entfernt oder Relay-Neustart nicht aufgerufen (called=%v)", called)
	}
	if got := oldBinaries(exe); len(got) != 0 {
		t.Fatalf("alte Binaries liegen noch: %v", got)
	}
}
