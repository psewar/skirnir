package main

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

// Selbst-Update ueber den Router (0.8.0; Router agentupdate.py): der Router schickt durch den Tunnel einen Auftrag mit
// Version, Dateiname, Download-URL, Einmal-Token, dem signierten Manifest (kompaktes JSON) und der Signatur des
// Betreiber-Schluessels. Der Agent prueft ZUERST die Signatur (Ed25519, oeffentlicher Schluessel in update.public_key),
// dann ob Datei/OS/Arch/SHA-256 im Manifest stehen, laedt die Binary neben sich, prueft den Hash, laesst sie `version`
// sagen, tauscht (laufende Datei wird umbenannt, geht unter Windows) und startet den Dienst neu. Die alte Binary bleibt
// als .old liegen; beim naechsten Start raeumt Cleanup auf. Ein kompromittierter Router allein kann so keinen Code
// auf den Knoten bringen - ohne den privaten Schluessel des Betreibers ist kein Manifest gueltig.

type UpdateCfg struct {
	Enabled   *bool  `yaml:"enabled"`
	PublicKey string `yaml:"public_key"` // base64, Ed25519; deploy.py --agent zeigt ihn nach dem Signieren
}

func (c *UpdateCfg) on() bool { return c.Enabled == nil || *c.Enabled }

// UpdateOrder: Steuernachricht {"t":"update", ...} des Routers.
type UpdateOrder struct {
	Version   string
	File      string
	URL       string
	Sha256    string
	Size      int64
	Token     string
	Manifest  string // kompaktes JSON, genau die signierten Bytes
	Signature string // base64
}

// UpdateReport geht mit dem Heartbeat an den Router (Block `update`).
type UpdateReport struct {
	Version string `json:"version"`
	State   string `json:"state"` // downloading | applied | failed
	Message string `json:"message,omitempty"`
	TS      string `json:"ts"`
}

type Updater struct {
	cfg     UpdateCfg
	cfgPath string
	log     *Logger
	mu      sync.Mutex
	busy    bool
	report  *UpdateReport
	restart func(exe string) // plattformspezifisch (updater_windows.go / updater_other.go)
}

func newUpdater(cfg UpdateCfg, cfgPath string, log *Logger) *Updater {
	u := &Updater{cfg: cfg, cfgPath: cfgPath, log: log}
	u.restart = func(exe string) { restartSelf(exe, cfgPath, log) }
	return u
}

func (u *Updater) Report() *UpdateReport {
	if u == nil {
		return nil
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.report == nil {
		return nil
	}
	r := *u.report
	return &r
}

func (u *Updater) set(state, version, msg string) {
	u.mu.Lock()
	u.report = &UpdateReport{Version: version, State: state, Message: msg, TS: time.Now().Format(time.RFC3339)}
	u.mu.Unlock()
}

// Handle nimmt einen Auftrag an; ein Auftrag zur Zeit, der Rest passiert im Hintergrund.
func (u *Updater) Handle(o UpdateOrder) {
	u.mu.Lock()
	if u.busy {
		u.mu.Unlock()
		u.log.Warnf("update: Auftrag auf %s verworfen, es laeuft schon einer", o.Version)
		return
	}
	u.busy = true
	u.mu.Unlock()
	go func() {
		defer func() {
			u.mu.Lock()
			u.busy = false
			u.mu.Unlock()
		}()
		u.log.Infof("update: Auftrag auf %s (%s, %d Bytes)", o.Version, o.File, o.Size)
		if err := u.run(o); err != nil {
			u.set("failed", o.Version, err.Error())
			u.log.Warnf("update: fehlgeschlagen: %v", err)
		}
	}()
}

type manifestFile struct {
	Name   string `json:"name"`
	OS     string `json:"os"`
	Arch   string `json:"arch"`
	Sha256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// verifyOrder prueft Signatur und Manifest und liefert den passenden Dateieintrag.
func (u *Updater) verifyOrder(o UpdateOrder) (*manifestFile, error) {
	if !u.cfg.on() {
		return nil, errors.New("Update per Konfiguration abgewaehlt (update.enabled: false)")
	}
	if o.Version == version {
		return nil, fmt.Errorf("laeuft schon mit %s", version)
	}
	if u.cfg.PublicKey == "" {
		return nil, errors.New("kein update.public_key konfiguriert - Manifest nicht pruefbar")
	}
	pub, err := base64.StdEncoding.DecodeString(u.cfg.PublicKey)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return nil, errors.New("update.public_key ist kein Ed25519-Schluessel (32 Bytes base64)")
	}
	sig, err := base64.StdEncoding.DecodeString(o.Signature)
	if err != nil || !ed25519.Verify(ed25519.PublicKey(pub), []byte(o.Manifest), sig) {
		return nil, errors.New("Manifest-Signatur ungueltig - Auftrag abgelehnt")
	}
	var m struct {
		Version string         `json:"version"`
		Files   []manifestFile `json:"files"`
	}
	if err := json.Unmarshal([]byte(o.Manifest), &m); err != nil {
		return nil, fmt.Errorf("Manifest unlesbar: %w", err)
	}
	if m.Version != o.Version {
		return nil, fmt.Errorf("Auftrag nennt %s, Manifest %s", o.Version, m.Version)
	}
	for i := range m.Files {
		f := &m.Files[i]
		if f.Name == o.File && f.OS == runtime.GOOS && f.Arch == runtime.GOARCH {
			if !strings.EqualFold(f.Sha256, o.Sha256) {
				return nil, errors.New("SHA-256 im Auftrag widerspricht dem Manifest")
			}
			return f, nil
		}
	}
	return nil, fmt.Errorf("%s fuer %s/%s steht nicht im signierten Manifest", o.File, runtime.GOOS, runtime.GOARCH)
}

// download holt die Datei mit dem Einmal-Token und prueft Groesse und SHA-256.
func (u *Updater) download(ctx context.Context, url, token, dst string, want *manifestFile) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := (&http.Client{Timeout: 10 * time.Minute}).Do(req)
	if err != nil {
		return fmt.Errorf("Download: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 300))
		return fmt.Errorf("Download: HTTP %d %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	f, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return fmt.Errorf("Zieldatei: %w", err)
	}
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, h), io.LimitReader(resp.Body, 512<<20))
	f.Close()
	if err != nil {
		os.Remove(dst)
		return fmt.Errorf("Download abgebrochen: %w", err)
	}
	if want.Size > 0 && n != want.Size {
		os.Remove(dst)
		return fmt.Errorf("Groesse %d statt %d Bytes", n, want.Size)
	}
	if got := hex.EncodeToString(h.Sum(nil)); !strings.EqualFold(got, want.Sha256) {
		os.Remove(dst)
		return fmt.Errorf("SHA-256 stimmt nicht (%s...)", got[:16])
	}
	return nil
}

func (u *Updater) run(o UpdateOrder) error {
	entry, err := u.verifyOrder(o)
	if err != nil {
		return err
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	if p, err := filepath.EvalSymlinks(exe); err == nil {
		exe = p
	}
	newPath := exe + ".new"
	u.set("downloading", o.Version, "")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	if err := u.download(ctx, o.URL, o.Token, newPath, entry); err != nil {
		return err
	}
	u.log.Infof("update: %s geladen und geprueft (SHA-256 %s...)", entry.Name, entry.Sha256[:16])
	// Rauchtest: die neue Binary muss ihre Version nennen (nach Signatur- und Hashpruefung, nie davor)
	cmd := exec.Command(newPath, "version")
	hideWindow(cmd)
	out, err := cmd.Output()
	if err != nil || strings.TrimSpace(string(out)) != o.Version {
		os.Remove(newPath)
		return fmt.Errorf("neue Binary meldet %q statt %s (%v)", strings.TrimSpace(string(out)), o.Version, err)
	}
	// Tausch: laufende Datei umbenennen (geht unter Windows und Linux), neue an ihren Platz; bei Fehler zurueck
	old := exe + ".old"
	if err := os.Remove(old); err != nil && !os.IsNotExist(err) {
		// .old vom letzten Update ist noch in Benutzung (gesehen 2026-09-25): unter einem anderen Namen parken statt scheitern
		old = fmt.Sprintf("%s.old-%d", exe, time.Now().Unix())
	}
	if err := os.Rename(exe, old); err != nil {
		os.Remove(newPath)
		return fmt.Errorf("alte Binary umbenennen: %w", err)
	}
	if err := os.Rename(newPath, exe); err != nil {
		_ = os.Rename(old, exe)
		return fmt.Errorf("neue Binary einsetzen: %w", err)
	}
	u.set("applied", o.Version, "Binary getauscht, Neustart")
	u.log.Infof("update: %s -> %s eingesetzt, Dienst startet neu", version, o.Version)
	time.Sleep(3 * time.Second) // ein Heartbeat mit 'applied' soll noch raus
	u.restart(exe)
	return nil
}

// Cleanup beim Start: Reste eines Tauschs entfernen (.old, .old-<ts>, .new); unter Windows das GPU-Z-Relay neu starten,
// das noch mit der alten Binary laeuft.
func (u *Updater) Cleanup() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	if p, err := filepath.EvalSymlinks(exe); err == nil {
		exe = p
	}
	_ = os.Remove(exe + ".new")
	if len(oldBinaries(exe)) == 0 {
		return
	}
	go cleanupOld(exe, func() { afterSwap(exe, u.log) }, 5*time.Second, u.log)
}

// oldBinaries: <exe>.old und geparkte <exe>.old-<ts> (updater.go run: gesperrte .old wird unter Zeitstempel geparkt).
func oldBinaries(exe string) []string {
	m, _ := filepath.Glob(exe + ".old*")
	return m
}

// cleanupOld: Relay neu starten (after), dann alle alten Binaries entfernen; der alte Dienstprozess und das alte Relay
// geben die Datei erst nach ihrem Ende frei, darum bis zu 12 Versuche im Abstand von wait. Liefert true, wenn alles weg ist.
func cleanupOld(exe string, after func(), wait time.Duration, log *Logger) bool {
	if after != nil {
		after()
	}
	for i := 0; i < 12; i++ {
		left := 0
		for _, p := range oldBinaries(exe) {
			if err := os.Remove(p); err != nil {
				left++
			}
		}
		if left == 0 {
			log.Infof("update: laeuft mit %s, alte Binary entfernt", version)
			return true
		}
		time.Sleep(wait)
	}
	log.Warnf("update: %s.old* liess sich nicht entfernen (noch in Benutzung?) - beim naechsten Start erneut", filepath.Base(exe))
	return false
}
