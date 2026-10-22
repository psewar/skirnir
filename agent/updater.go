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
	"syscall"
	"time"
)

// Self-update via the router (0.8.0; router agentupdate.py): the router sends an order through the tunnel with
// version, filename, download URL, one-time token, the signed manifest (compact JSON), and the signature of the
// operator key. The agent checks the signature FIRST (Ed25519, public key in update.public_key),
// then verifies file/OS/arch/SHA-256 in the manifest, downloads the binary next to itself, checks the hash, runs `version`
// to verify, swaps (running file is renamed, works on Windows), and restarts the service. The old binary remains
// as .old; cleanup removes it on the next start. A compromised router alone cannot push code
// to the node - without the operator's private key, no manifest is valid.

type UpdateCfg struct {
	Enabled   *bool  `yaml:"enabled"`
	PublicKey string `yaml:"public_key"` // base64, Ed25519; deploy.py --agent displays it after signing
}

func (c *UpdateCfg) on() bool { return c.Enabled == nil || *c.Enabled }

// UpdateOrder: control message {"t":"update", ...} from the router.
type UpdateOrder struct {
	Version   string
	File      string
	URL       string
	Sha256    string
	Size      int64
	Token     string
	Manifest  string // compact JSON, exactly the signed bytes
	Signature string // base64
}

// UpdateReport is sent to the router with the heartbeat (block `update`).
type UpdateReport struct {
	Version string `json:"version"`
	State   string `json:"state"` // downloading | applied | failed
	Message string `json:"message,omitempty"`
	// Kind: fixed error type (0.19.1), "" for no specific type. Always sent, even if empty: this lets the router detect the
	// presence of the field and avoid text-searching the message (agentupdate.retry_after).
	Kind string `json:"kind"`
	TS   string `json:"ts"`
}

// updateKindAppControl: Smart App Control or a WDAC policy blocked the new binary. The router retries
// after 30 min, as the cloud verdict on a new file often flips within minutes to hours.
const updateKindAppControl = "app_control"

// appControlBlocked: Windows-Fehlercodes "An Application Control policy has blocked this file" (winerror.h,
// ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION 4551, _REPUTATION_MALICIOUS/_PUA/_DANGEROUS_EXT/_OFFLINE 4556-4559,
// _REPUTATION_UNFRIENDLY_FILE/_UNATTAINABLE/_EXPLICIT_DENY_FILE/_WHQL_FAILURE 4580-4583), with which CreateProcess rejects a
// blocked binary. Observed 2026-09-26 during smoke test `version` of unsigned 0.11.0.
var appControlBlocked = []syscall.Errno{4551, 4556, 4557, 4558, 4559, 4580, 4581, 4582, 4583}

// updateKind maps a failure at the Windows error code to a fixed type (never the localized message text).
func updateKind(err error) string {
	for _, code := range appControlBlocked {
		if errors.Is(err, code) {
			return updateKindAppControl
		}
	}
	return ""
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

func (u *Updater) set(state, version, msg, kind string) {
	u.mu.Lock()
	u.report = &UpdateReport{Version: version, State: state, Message: msg, Kind: kind, TS: time.Now().Format(time.RFC3339)}
	u.mu.Unlock()
}

// Handle accepts a request; one request at a time, the rest happens in the background.
func (u *Updater) Handle(o UpdateOrder) {
	u.mu.Lock()
	if u.busy {
		u.mu.Unlock()
		u.log.Warnf("update: order for %s dropped, one is already running", o.Version)
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
		u.log.Infof("update: order for %s (%s, %d bytes)", o.Version, o.File, o.Size)
		if err := u.run(o); err != nil {
			u.set("failed", o.Version, err.Error(), updateKind(err))
			u.log.Warnf("update: failed: %v", err)
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

// verifyOrder checks signature and manifest and returns the matching file entry.
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

// download fetches the file with the one-time token and checks size and SHA-256.
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
	u.set("downloading", o.Version, "", "")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	if err := u.download(ctx, o.URL, o.Token, newPath, entry); err != nil {
		return err
	}
	u.log.Infof("update: %s downloaded and verified (SHA-256 %s...)", entry.Name, entry.Sha256[:16])
	// Smoke test: the new binary must report its version (after signature and hash check, never before)
	if err := smokeTest(newPath, o.Version); err != nil {
		os.Remove(newPath)
		return err
	}
	// ... and it must accept the existing configuration: otherwise the swapped service restarts in a loop, instead of the
	// order ending cleanly as failed (e.g. after removed config keys, 0.9.1: tunnel.enabled, mimir:)
	if u.cfgPath != "" {
		chk := exec.Command(newPath, "check-config", "--config", u.cfgPath)
		hideWindow(chk)
		if out, err := chk.CombinedOutput(); err != nil {
			os.Remove(newPath)
			return fmt.Errorf("neue Binary lehnt die Konfiguration ab: %s", strings.TrimSpace(string(out)))
		}
	}
	// Swap: rename running file (works on Windows and Linux), move new one into place; revert on error
	old := exe + ".old"
	if err := os.Remove(old); err != nil && !os.IsNotExist(err) {
		// .old from last update is still in use (observed 2026-09-25): park under a different name instead of failing
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
	u.set("applied", o.Version, "Binary getauscht, Neustart", "")
	u.log.Infof("update: %s -> %s installed, service restarts", version, o.Version)
	time.Sleep(3 * time.Second) // one heartbeat with 'applied' should still go out
	u.restart(exe)
	return nil
}

// smokeTest: the binary at path must answer `version` with want. A start error is wrapped with %w so that
// updateKind finds the Windows error code (Smart App Control / WDAC blocks CreateProcess).
func smokeTest(path, want string) error {
	cmd := exec.Command(path, "version")
	hideWindow(cmd)
	out, err := cmd.Output()
	got := strings.TrimSpace(string(out))
	if err != nil {
		return fmt.Errorf("neue Binary meldet %q statt %s: %w", got, want, err)
	}
	if got != want {
		return fmt.Errorf("neue Binary meldet %q statt %s", got, want)
	}
	return nil
}

// Cleanup on start: remove swap leftovers (.old, .old-<ts>, .new); on Windows restart the GPU-Z relay,
// which is still running with the old binary.
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

// oldBinaries: <exe>.old and parked <exe>.old-<ts> (updater.go run: locked .old is parked with timestamp).
func oldBinaries(exe string) []string {
	m, _ := filepath.Glob(exe + ".old*")
	return m
}

// cleanupOld: restart relay (after), then remove all old binaries; the old service process and old relay
// release the file only after they exit, so up to 12 attempts spaced by wait. Returns true if all are gone.
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
			log.Infof("update: running %s, old binary removed", version)
			return true
		}
		time.Sleep(wait)
	}
	log.Warnf("update: %s.old* could not be removed (still in use?) - retrying at next start", filepath.Base(exe))
	return false
}
