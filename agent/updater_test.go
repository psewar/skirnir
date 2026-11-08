package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
)

// signedOrder builds manifest + signature like deploy.py --agent (compact JSON, sorted keys).
func signedOrder(t *testing.T, priv ed25519.PrivateKey, ver, file, sum string) UpdateOrder {
	t.Helper()
	m := map[string]any{"version": ver, "generated": "2026-09-25T10:00:00",
		"files": []map[string]any{{"arch": runtime.GOARCH, "name": file, "os": runtime.GOOS, "sha256": sum, "size": 3}}}
	b, _ := json.Marshal(m) // Go sorts map keys: equivalent to sort_keys + separators without spaces
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
	// Manifest modified after the fact
	bad := o
	bad.Manifest = strings.Replace(o.Manifest, "9.9.9", "9.9.8", 1)
	if _, err := u.verifyOrder(bad); err == nil {
		t.Fatal("veraendertes Manifest akzeptiert")
	}
	// Hash in the order contradicts the manifest
	bad = o
	bad.Sha256 = strings.Repeat("0", 64)
	if _, err := u.verifyOrder(bad); err == nil || !strings.Contains(err.Error(), "SHA-256") {
		t.Fatalf("Hash-Widerspruch akzeptiert: %v", err)
	}
	// File not for this OS in the manifest
	bad = o
	bad.File = "andere-datei"
	if _, err := u.verifyOrder(bad); err == nil {
		t.Fatal("fremde Datei akzeptiert")
	}
	// without key: never
	u2 := newUpdater(UpdateCfg{}, "", nil)
	if _, err := u2.verifyOrder(o); err == nil || !strings.Contains(err.Error(), "public_key") {
		t.Fatalf("ohne public_key akzeptiert: %v", err)
	}
	// deselected
	off := false
	u3 := newUpdater(UpdateCfg{Enabled: &off, PublicKey: base64.StdEncoding.EncodeToString(pub)}, "", nil)
	if _, err := u3.verifyOrder(o); err == nil || !strings.Contains(err.Error(), "abgewaehlt") {
		t.Fatalf("abgewaehlt akzeptiert: %v", err)
	}
}

// Review 2026-09-25: a locked .old is parked as .old-<ts> during swap; Cleanup only knew .old and left them
// parked files remain - and the Relay did not restart exactly then.
func TestUpdaterCleanupRemovesParkedOldBinaries(t *testing.T) {
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
	log, err := newLogger(LogCfg{Dir: t.TempDir()}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	// Windows does not delete open files: without closing, TempDir cleanup fails.
	t.Cleanup(func() { log.file.Close() })
	called := false
	if !cleanupOld(exe, func() { called = true }, time.Millisecond, log) || !called {
		t.Fatalf("cleanupOld: nicht alles entfernt oder Relay-Neustart nicht aufgerufen (called=%v)", called)
	}
	if got := oldBinaries(exe); len(got) != 0 {
		t.Fatalf("alte Binaries liegen noch: %v", got)
	}
}

// 0.19.1: a binary blocked by Smart App Control / WDAC is detected by the agent via the Windows error code from CreateProcess
// liefert, nie am (lokalisierten) Meldungstext.
func TestUpdateKind(t *testing.T) {
	start := func(code syscall.Errno) error {
		// so the error comes from smokeTest: exec -> os.PathError -> wrapped with %w
		return fmt.Errorf("neue Binary meldet %q statt %s: %w", "", "9.9.9",
			&os.PathError{Op: "fork/exec", Path: "skirnir-agent.exe.new", Err: code})
	}
	for _, code := range appControlBlocked {
		if got := updateKind(start(code)); got != updateKindAppControl {
			t.Errorf("Code %d: %q statt %q", uint(code), got, updateKindAppControl)
		}
	}
	for name, err := range map[string]error{
		"anderer Code":         start(syscall.Errno(2)),
		"Richtlinie ungueltig": start(syscall.Errno(4552)), // Application Control, but no file block
		"nur Text":             errors.New("neue Binary meldet \"\" statt 9.9.9: An Application Control policy has blocked this file."),
		"Text deutsch":         errors.New("Diese Datei wurde von einer Anwendungssteuerungsrichtlinie blockiert."),
		"ohne Code eingepackt": fmt.Errorf("neue Binary meldet %q statt %s", "1.0.0", "9.9.9"),
		"kein Fehler":          nil,
	} {
		if got := updateKind(err); got != "" {
			t.Errorf("%s: %q statt leer", name, got)
		}
	}
}

// The smoke test start error remains wrapped, otherwise updateKind would not find the code.
func TestSmokeTestKeepsStartError(t *testing.T) {
	err := smokeTest(filepath.Join(t.TempDir(), "fehlt.exe"), "9.9.9")
	if !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("Startfehler nicht eingepackt: %v", err)
	}
}

// The router detects via the presence of `kind` that the agent reports the error type: the field may also be empty.
func TestUpdateReportKindAlwaysSent(t *testing.T) {
	b, _ := json.Marshal(UpdateReport{Version: "9.9.9", State: "failed", Message: "x"})
	if !strings.Contains(string(b), `"kind":""`) {
		t.Fatalf("kind fehlt im Bericht: %s", b)
	}
}

// ADR-0054 / TM-13: a validly signed but older manifest must not roll a release back.
func TestDowngradeRefusal(t *testing.T) {
	cases := []struct {
		running, want  string
		allow, refused bool
	}{
		{"0.27.0", "0.28.0", false, false},
		{"0.27.0", "0.27.1", false, false},
		{"0.27.0", "1.0.0", false, false},
		{"0.27.0", "0.26.9", false, true},
		{"0.27.0", "0.9.99", false, true},
		{"0.27.0", "dev", false, true},
		{"0.27.0", "0.28.0-rc1", false, true},
		{"0.27.0", "0.26.9", true, false},
		{"dev", "0.1.0", false, false},
	}
	for _, c := range cases {
		err := downgradeRefusal(c.running, c.want, c.allow)
		if (err != nil) != c.refused {
			t.Fatalf("%s -> %s allow=%v: refused=%v, want %v (%v)", c.running, c.want, c.allow, err != nil, c.refused, err)
		}
		if err != nil && updateKind(err) != updateKindDowngrade {
			t.Fatalf("%s -> %s: kind %q, want %q", c.running, c.want, updateKind(err), updateKindDowngrade)
		}
	}
}

func TestUpdaterVerifyOrderRefusesSignedDowngrade(t *testing.T) {
	prev := version
	version = "0.27.0"
	defer func() { version = prev }()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	sum := hex.EncodeToString(sha256.New().Sum(nil))
	key := base64.StdEncoding.EncodeToString(pub)
	old := signedOrder(t, priv, "0.26.1", "skirnir-agent-0.26.1-test", sum)
	if _, err := newUpdater(UpdateCfg{PublicKey: key}, "", nil).verifyOrder(old); !errors.Is(err, errDowngrade) {
		t.Fatalf("signed older manifest accepted: %v", err)
	}
	if _, err := newUpdater(UpdateCfg{PublicKey: key, AllowDowngrade: true}, "", nil).verifyOrder(old); err != nil {
		t.Fatalf("explicit allow_downgrade refused: %v", err)
	}
	if _, err := newUpdater(UpdateCfg{PublicKey: key}, "", nil).verifyOrder(signedOrder(t, priv, "0.28.0", "skirnir-agent-0.28.0-test", sum)); err != nil {
		t.Fatalf("newer manifest refused: %v", err)
	}
}

// ADR-0058 / TM-13: a signing key can be rotated - any configured operator key verifies, nothing else does.
func TestUpdaterAcceptsAnyConfiguredKeyForRotation(t *testing.T) {
	oldPub, oldPriv, _ := ed25519.GenerateKey(rand.Reader)
	newPub, newPriv, _ := ed25519.GenerateKey(rand.Reader)
	_, strangerPriv, _ := ed25519.GenerateKey(rand.Reader)
	sum := hex.EncodeToString(sha256.New().Sum(nil))
	enc := base64.StdEncoding.EncodeToString
	both := UpdateCfg{PublicKey: enc(oldPub), PublicKeys: []string{enc(newPub)}}
	for label, priv := range map[string]ed25519.PrivateKey{"old key": oldPriv, "new key": newPriv} {
		if _, err := newUpdater(both, "", nil).verifyOrder(signedOrder(t, priv, "9.9.9", "skirnir-agent-9.9.9-test", sum)); err != nil {
			t.Fatalf("manifest signed with the %s refused during rotation: %v", label, err)
		}
	}
	if _, err := newUpdater(both, "", nil).verifyOrder(signedOrder(t, strangerPriv, "9.9.9", "skirnir-agent-9.9.9-test", sum)); err == nil || !strings.Contains(err.Error(), "Signatur") {
		t.Fatalf("manifest signed with an unlisted key accepted: %v", err)
	}
	onlyNew := UpdateCfg{PublicKeys: []string{enc(newPub)}}
	if _, err := newUpdater(onlyNew, "", nil).verifyOrder(signedOrder(t, oldPriv, "9.9.9", "skirnir-agent-9.9.9-test", sum)); err == nil {
		t.Fatal("old key still accepted after it was removed from the configuration")
	}
	if _, err := newUpdater(onlyNew, "", nil).verifyOrder(signedOrder(t, newPriv, "9.9.9", "skirnir-agent-9.9.9-test", sum)); err != nil {
		t.Fatalf("public_keys alone refused: %v", err)
	}
	typo := UpdateCfg{PublicKey: enc(oldPub), PublicKeys: []string{"not-a-key"}}
	if _, err := newUpdater(typo, "", nil).verifyOrder(signedOrder(t, oldPriv, "9.9.9", "skirnir-agent-9.9.9-test", sum)); err == nil || !strings.Contains(err.Error(), "Eintrag 1") {
		t.Fatalf("an invalid entry in public_keys was not reported: %v", err)
	}
}
