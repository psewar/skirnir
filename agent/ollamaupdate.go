package main

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"sync"
	"time"
)

// Ollama update via the router (0.12.0; router ollamaupdate.py). Since Ollama runs as a child of the service, the
// tray app updater no longer works: it runs as the user, and the installer is a per-user installer (under
// LocalSystem it ended up in the SYSTEM profile). The router checks the latest version on GitHub and sends in the night window,
// if the node is free, {"t":"ollama-update", version, file, sha256, size}.
//
// The agent downloads the release archive (ollama.exe + lib/ollama, no installer) ITSELF from the fixed source
// (ollama_update.source, default: GitHub releases of ollama/ollama), fetches sha256sum.txt there and compares with the
// request - so the router can only choose the version, not the code. Then: extract to a staging folder next to
// the installation, `ollama --version` of the new binary, stop child, move ollama.exe + lib to a backup, new
// files in place, start child; /api/version must report the target version, otherwise roll back to the backup.
// Tray app and Windows uninstall entry remain on the old version (cosmetic).

type OllamaUpdateCfg struct {
	Enabled *bool  `yaml:"enabled"`
	Source  string `yaml:"source"` // Release URL prefix: <source>v<version>/<file> and <source>v<version>/sha256sum.txt
}

const ollamaSourceDefault = "https://github.com/ollama/ollama/releases/download/"

func (c *OllamaUpdateCfg) on() bool { return c.Enabled == nil || *c.Enabled }

func (c *OllamaUpdateCfg) source() string {
	s := c.Source
	if s == "" {
		s = ollamaSourceDefault
	}
	if !strings.HasSuffix(s, "/") {
		s += "/"
	}
	return s
}

// OllamaUpdateOrder: Control message {"t":"ollama-update", ...} from the router.
type OllamaUpdateOrder struct {
	Version string
	File    string
	Sha256  string
	Size    int64
}

type OllamaUpdater struct {
	cfg      OllamaUpdateCfg
	sup      *Supervisor
	log      *Logger
	child    string // Name of the child that is Ollama ("" = none -> requests are rejected)
	dir      string // Installation folder (folder of ollama.exe)
	upstream string // http://127.0.0.1:11434 - /api/version after the swap

	mu     sync.Mutex
	busy   bool
	report *UpdateReport

	// swappable for tests
	archive    func() (string, error)            // Archive name for this system
	probe      func(exe string) (string, error)  // Version of the extracted binary (`ollama --version`)
	live       func(ctx context.Context) string  // Version of the running Ollama (/api/version)
	free       func(path string) (uint64, error) // free space on the data drive
	http       *http.Client
	verifyWait time.Duration // how long the new Ollama may take until /api/version reports the target version
	verifyPoll time.Duration
	swapWait   time.Duration // how long a fully downloaded update waits for router approval (0.15.0)
	swapCh     chan string   // Approval {"t":"ollama-swap", version} from the router
}

func newOllamaUpdater(cfg OllamaUpdateCfg, specs []ChildSpec, sup *Supervisor, upstream string, log *Logger) *OllamaUpdater {
	u := &OllamaUpdater{cfg: cfg, sup: sup, upstream: upstream, log: log, http: &http.Client{Timeout: 90 * time.Minute},
		verifyWait: 3 * time.Minute, verifyPoll: 2 * time.Second, swapWait: 30 * time.Minute, swapCh: make(chan string, 1)}
	for _, sp := range specs {
		if sp.on() && isOllamaExe(sp.Cmd) {
			u.child, u.dir = sp.Name, filepath.Dir(sp.Cmd)
			break
		}
	}
	u.archive = ollamaArchiveName
	u.probe = probeOllamaVersion
	u.live = func(ctx context.Context) string { return ollamaVersion(ctx, upstream) }
	u.free = diskFree
	return u
}

func isOllamaExe(cmd string) bool {
	b := cmd
	if i := strings.LastIndexAny(cmd, `\/`); i >= 0 { // both separators, independent of the caller's platform
		b = cmd[i+1:]
	}
	b = strings.ToLower(b)
	return b == "ollama.exe" || b == "ollama"
}

// SwapGate: the agent swaps only after router approval (0.15.0, fact ollama_swap_gate). Until then Ollama
// continues to run normally during download and check and serves requests; only the router knows when the node is idle.
func (u *OllamaUpdater) SwapGate() bool { return u.Managed() }

// Swap: Router approval {"t":"ollama-swap", version}. Only affects a pending request of the same version.
func (u *OllamaUpdater) Swap(version string) {
	if u == nil {
		return
	}
	select {
	case u.swapCh <- version:
	default:
	}
}

// awaitSwap waits for router approval; without it, nothing is swapped.
func (u *OllamaUpdater) awaitSwap(ctx context.Context, version string) error {
	t := time.NewTimer(u.swapWait)
	defer t.Stop()
	for {
		select {
		case v := <-u.swapCh:
			if v == version {
				return nil
			}
			u.log.Warnf("ollama-update: release for %s ignored, the order is %s", v, version)
		case <-t.C:
			return fmt.Errorf("Router gab den Tausch nicht innerhalb %s frei - nichts getauscht, Ollama laeuft unveraendert", u.swapWait)
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Managed: does Ollama run as a child of this agent (then the router can trigger an update)?
func (u *OllamaUpdater) Managed() bool { return u != nil && u.child != "" }

func (u *OllamaUpdater) Report() *UpdateReport {
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

func (u *OllamaUpdater) set(state, version, msg string) {
	u.mu.Lock()
	u.report = &UpdateReport{Version: version, State: state, Message: msg, TS: time.Now().Format(time.RFC3339)}
	u.mu.Unlock()
	if u.log != nil {
		u.log.Infof("ollama-update: %s %s %s", version, state, msg)
	}
}

// Handle accepts a request; one request at a time, the rest happens in the background.
func (u *OllamaUpdater) Handle(o OllamaUpdateOrder) {
	u.mu.Lock()
	if u.busy {
		u.mu.Unlock()
		u.log.Warnf("ollama-update: order for %s dropped, one is already running", o.Version)
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
		u.log.Infof("ollama-update: order for %s (%s, %d MB)", o.Version, o.File, o.Size>>20)
		if err := u.run(o); err != nil {
			u.set("failed", o.Version, err.Error())
			u.log.Warnf("ollama-update: failed: %v", err)
		}
	}()
}

// ollamaArchiveName: Release archive for this system. Linux archives are tar.zst (Zstandard is not in the
// Go standard library) - there the agent reports the request as unsupported.
func ollamaArchiveName() (string, error) {
	switch runtime.GOOS + "/" + runtime.GOARCH {
	case "windows/amd64":
		return "ollama-windows-amd64.zip", nil
	case "windows/arm64":
		return "ollama-windows-arm64.zip", nil
	}
	return "", fmt.Errorf("Ollama-Update fuer %s/%s noch nicht unterstuetzt (Linux-Archive sind tar.zst)", runtime.GOOS, runtime.GOARCH)
}

// ollamaExeName: Name of the binary in the archive (Zip = Windows).
func ollamaExeName(archive string) string {
	if strings.HasSuffix(archive, ".zip") {
		return "ollama.exe"
	}
	return "ollama"
}

var ollamaVersionRe = regexp.MustCompile(`^[0-9]+(\.[0-9]+){1,3}([-+][0-9A-Za-z.]+)?$`)

func (u *OllamaUpdater) verifyOrder(o OllamaUpdateOrder) error {
	if !u.cfg.on() {
		return errors.New("Ollama-Update per Konfiguration abgewaehlt (ollama_update.enabled: false)")
	}
	if u.child == "" {
		return errors.New("Ollama ist kein Kind dieses Agenten (children: ohne ollama.exe) - Update nur von Hand")
	}
	want, err := u.archive()
	if err != nil {
		return err
	}
	if o.File != want {
		return fmt.Errorf("Auftrag nennt %s, fuer dieses System passt %s", o.File, want)
	}
	if !ollamaVersionRe.MatchString(o.Version) {
		return fmt.Errorf("Version %q unbrauchbar", o.Version)
	}
	if len(o.Sha256) != 64 {
		return errors.New("Auftrag ohne SHA-256")
	}
	return nil
}

// fetchChecksum: Fetch sha256sum.txt of the release from the fixed source and return the entry for the file.
func (u *OllamaUpdater) fetchChecksum(ctx context.Context, version, file string) (string, error) {
	url := u.cfg.source() + "v" + version + "/sha256sum.txt"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", err
	}
	resp, err := (&http.Client{Timeout: 60 * time.Second}).Do(req)
	if err != nil {
		return "", fmt.Errorf("sha256sum.txt: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("sha256sum.txt: HTTP %d (%s)", resp.StatusCode, url)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	return parseChecksum(string(body), file)
}

// parseChecksum: Lines "<hex>  ./<file>" (GNU format, also "*<file>" or without prefix).
func parseChecksum(txt, file string) (string, error) {
	for _, l := range strings.Split(txt, "\n") {
		f := strings.Fields(l)
		if len(f) != 2 || len(f[0]) != 64 {
			continue
		}
		name := strings.TrimPrefix(strings.TrimPrefix(f[1], "./"), "*")
		if name == file {
			return strings.ToLower(f[0]), nil
		}
	}
	return "", fmt.Errorf("%s steht nicht in sha256sum.txt", file)
}

// download fetches the archive, checks size and SHA-256 and reports progress in ten-percent steps.
func (u *OllamaUpdater) download(ctx context.Context, url, dst string, size int64, sum, version string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := u.http.Do(req)
	if err != nil {
		return fmt.Errorf("Download: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("Download: HTTP %d (%s)", resp.StatusCode, url)
	}
	f, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("Zieldatei: %w", err)
	}
	h := sha256.New()
	var n int64
	buf := make([]byte, 1<<20)
	step := 0
	src := io.LimitReader(resp.Body, 4<<30)
	for {
		k, rerr := src.Read(buf)
		if k > 0 {
			if _, werr := f.Write(buf[:k]); werr != nil {
				f.Close()
				os.Remove(dst)
				return fmt.Errorf("schreiben: %w", werr)
			}
			h.Write(buf[:k])
			n += int64(k)
			if size > 0 {
				if pct := int(n * 10 / size); pct > step {
					step = pct
					u.set("downloading", version, fmt.Sprintf("%d %% von %d MB", pct*10, size>>20))
				}
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			f.Close()
			os.Remove(dst)
			return fmt.Errorf("Download abgebrochen: %w", rerr)
		}
	}
	f.Close()
	if size > 0 && n != size {
		os.Remove(dst)
		return fmt.Errorf("Groesse %d statt %d Bytes", n, size)
	}
	if got := hex.EncodeToString(h.Sum(nil)); !strings.EqualFold(got, sum) {
		os.Remove(dst)
		return fmt.Errorf("SHA-256 stimmt nicht (%s...)", got[:16])
	}
	return nil
}

// extractZip extracts to dst; paths leading out of dst are rejected (Zip-Slip).
func extractZip(src, dst string) error {
	r, err := zip.OpenReader(src)
	if err != nil {
		return err
	}
	defer r.Close()
	root := filepath.Clean(dst) + string(filepath.Separator)
	for _, f := range r.File {
		p := filepath.Join(dst, filepath.FromSlash(f.Name))
		if !strings.HasPrefix(p, root) {
			return fmt.Errorf("unzulaessiger Pfad im Archiv: %s", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(p, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			return err
		}
		if err := extractOne(f, p); err != nil {
			return fmt.Errorf("%s: %w", f.Name, err)
		}
	}
	return nil
}

func extractOne(f *zip.File, p string) error {
	in, err := f.Open()
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(p, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, f.Mode()|0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// probeOllamaVersion: `ollama --version` of the extracted binary. OLLAMA_HOST set to a dead port so it does not query the
// running server but reports its own version ("Warning: client version is X").
func probeOllamaVersion(exe string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, exe, "--version")
	cmd.Env = append(os.Environ(), "OLLAMA_HOST=127.0.0.1:1")
	hideWindow(cmd)
	out, _ := cmd.CombinedOutput()
	return parseOllamaVersion(string(out))
}

var clientVersionRe = regexp.MustCompile(`client version is ([0-9][0-9A-Za-z.+-]*)`)
var serverVersionRe = regexp.MustCompile(`ollama version is ([0-9][0-9A-Za-z.+-]*)`)

// parseOllamaVersion: "client version is X" takes precedence (present as soon as client and reachable server differ
// or no server responds), otherwise "ollama version is X".
func parseOllamaVersion(out string) (string, error) {
	if m := clientVersionRe.FindStringSubmatch(out); m != nil {
		return m[1], nil
	}
	if m := serverVersionRe.FindStringSubmatch(out); m != nil {
		return m[1], nil
	}
	return "", fmt.Errorf("keine Version in %q", strings.TrimSpace(out))
}

// renameRetry: Rename with retry - a virus scanner or the just-terminated process may hold files briefly.
func renameRetry(from, to string) error {
	var err error
	for i := 0; i < 10; i++ {
		if err = os.Rename(from, to); err == nil {
			return nil
		}
		time.Sleep(time.Second)
	}
	return err
}

// swapTop places the top entries of the new folder (ollama.exe, lib) into dir; existing items move to backup.
// Returns the applied names (for rollbackTop).
func swapTop(dir, newDir, backup string) ([]string, error) {
	entries, err := os.ReadDir(newDir)
	if err != nil {
		return nil, err
	}
	var moved []string
	for _, e := range entries {
		name := e.Name()
		cur, nw, bk := filepath.Join(dir, name), filepath.Join(newDir, name), filepath.Join(backup, name)
		if _, err := os.Stat(cur); err == nil {
			if err := renameRetry(cur, bk); err != nil {
				return moved, fmt.Errorf("%s sichern: %w", name, err)
			}
		}
		if err := renameRetry(nw, cur); err != nil {
			_ = renameRetry(bk, cur)
			return moved, fmt.Errorf("%s einsetzen: %w", name, err)
		}
		moved = append(moved, name)
	}
	return moved, nil
}

// rollbackTop reverses swapTop: new files back to newDir, backup restored to its place.
func rollbackTop(dir, newDir, backup string, names []string) error {
	var first error
	for _, name := range names {
		cur, nw, bk := filepath.Join(dir, name), filepath.Join(newDir, name), filepath.Join(backup, name)
		if err := renameRetry(cur, nw); err != nil && !os.IsNotExist(err) && first == nil {
			first = err
		}
		if _, err := os.Stat(bk); err == nil {
			if err := renameRetry(bk, cur); err != nil && first == nil {
				first = err
			}
		}
	}
	return first
}

func (u *OllamaUpdater) hold() {
	if u.sup != nil {
		u.sup.Hold(u.child, 30*time.Second)
	}
}

func (u *OllamaUpdater) release() {
	if u.sup != nil {
		u.sup.Release(u.child)
	}
}

func (u *OllamaUpdater) run(o OllamaUpdateOrder) error {
	if err := u.verifyOrder(o); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Hour)
	defer cancel()
	for drained := false; !drained; { // Discard old release from a previous task
		select {
		case <-u.swapCh:
		default:
			drained = true
		}
	}
	u.set("checking", o.Version, "sha256sum.txt des Releases")
	sum, err := u.fetchChecksum(ctx, o.Version, o.File)
	if err != nil {
		return err
	}
	if !strings.EqualFold(sum, o.Sha256) {
		return errors.New("SHA-256 im Auftrag widerspricht sha256sum.txt des Releases - Auftrag abgelehnt")
	}
	parent := filepath.Dir(u.dir)
	staging := filepath.Join(parent, ".skirnir-ollama-update")
	os.RemoveAll(staging)
	if err := os.MkdirAll(staging, 0o755); err != nil {
		return fmt.Errorf("Staging-Ordner: %w", err)
	}
	defer os.RemoveAll(staging) // Success: old files removed; Error: new files removed (backup is already restored)
	if free, err := u.free(parent); err == nil && o.Size > 0 && free < uint64(o.Size)*3 {
		return fmt.Errorf("zu wenig Platz auf dem Datentraeger: %d MB frei, %d MB noetig (Archiv + entpackt)", free>>20, (o.Size*3)>>20)
	}
	u.set("downloading", o.Version, fmt.Sprintf("0 %% von %d MB", o.Size>>20))
	archive := filepath.Join(staging, o.File)
	if err := u.download(ctx, u.cfg.source()+"v"+o.Version+"/"+o.File, archive, o.Size, sum, o.Version); err != nil {
		return err
	}
	u.set("extracting", o.Version, "")
	newDir := filepath.Join(staging, "new")
	if err := extractZip(archive, newDir); err != nil {
		return fmt.Errorf("entpacken: %w", err)
	}
	os.Remove(archive)
	if v, err := u.probe(filepath.Join(newDir, ollamaExeName(o.File))); err != nil || v != o.Version {
		return fmt.Errorf("entpackte Binary meldet %q statt %s (%v)", v, o.Version, err)
	}
	// Up to this point, Ollama continued running unchanged. Now the agent reports "ready"; the router removes the node from routing,
	// waits until no requests are running, and releases the swap (2026-10-01: previously the node was locked during the entire
	// download, and a prewarm loaded a model that the swap immediately terminated).
	u.set("ready", o.Version, "geladen und geprueft - wartet auf die Freigabe des Routers")
	if err := u.awaitSwap(ctx, o.Version); err != nil {
		return err
	}
	// Tausch: Kind anhalten (samt Modellprozessen), Dateien umsetzen, Kind wieder freigeben
	u.set("swapping", o.Version, "Ollama wird angehalten")
	u.hold()
	backup := filepath.Join(staging, "old")
	if err := os.MkdirAll(backup, 0o755); err != nil {
		u.release()
		return err
	}
	moved, err := swapTop(u.dir, newDir, backup)
	if err != nil {
		rerr := rollbackTop(u.dir, newDir, backup, moved)
		u.release()
		return fmt.Errorf("Dateien tauschen: %v (zurueckgesetzt: %v)", err, rerr == nil)
	}
	u.release()
	u.set("applied", o.Version, "Dateien getauscht, Ollama startet neu")
	// Proof: the running Ollama must report the target version
	deadline := time.Now().Add(u.verifyWait)
	for time.Now().Before(deadline) {
		time.Sleep(u.verifyPoll)
		if v := u.live(ctx); v == o.Version {
			u.log.Infof("ollama-update: Ollama runs %s", v)
			u.syncRegistration(v) // Update Windows entry (Apps, winget), otherwise winget continues to offer the release
			return nil
		}
	}
	u.log.Warnf("ollama-update: Ollama does not report %s - back to the backup", o.Version)
	u.hold()
	rerr := rollbackTop(u.dir, newDir, backup, moved)
	u.release()
	return fmt.Errorf("neues Ollama meldete sich nicht innerhalb %s mit %s - zurueckgesetzt (%v)", u.verifyWait, o.Version, rerr == nil)
}
