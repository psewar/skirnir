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

// Ollama-Update ueber den Router (0.12.0; Router ollamaupdate.py). Seit Ollama als Kind des Dienstes laeuft, greift der
// Updater der Tray-App nicht mehr: er laeuft unter dem Benutzer, und der Installer ist ein Pro-Benutzer-Installer (unter
// LocalSystem landete er im SYSTEM-Profil). Der Router prueft die neueste Version auf GitHub und schickt im Nachtfenster,
// wenn der Knoten frei ist, {"t":"ollama-update", version, file, sha256, size}.
//
// Der Agent laedt das Release-Archiv (ollama.exe + lib/ollama, kein Installer) SELBST von der festen Quelle
// (ollama_update.source, Standard: GitHub-Releases von ollama/ollama), holt dort auch sha256sum.txt und vergleicht mit dem
// Auftrag - der Router kann also nur die Version waehlen, nicht den Code. Dann: entpacken in einen Staging-Ordner neben
// der Installation, `ollama --version` der neuen Binary, Kind anhalten, ollama.exe + lib in eine Sicherung schieben, neue
// Dateien einsetzen, Kind starten; /api/version muss die Zielversion melden, sonst zurueck auf die Sicherung.
// Tray-App und Deinstallationseintrag von Windows bleiben auf der alten Version stehen (kosmetisch).

type OllamaUpdateCfg struct {
	Enabled *bool  `yaml:"enabled"`
	Source  string `yaml:"source"` // URL-Praefix der Releases: <source>v<version>/<datei> und <source>v<version>/sha256sum.txt
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

// OllamaUpdateOrder: Steuernachricht {"t":"ollama-update", ...} des Routers.
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
	child    string // Name des Kindes, das Ollama ist ("" = keins -> Auftraege werden abgelehnt)
	dir      string // Installationsordner (Ordner der ollama.exe)
	upstream string // http://127.0.0.1:11434 - /api/version nach dem Tausch

	mu     sync.Mutex
	busy   bool
	report *UpdateReport

	// austauschbar fuer Tests
	archive    func() (string, error)            // Archivname fuer dieses System
	probe      func(exe string) (string, error)  // Version der entpackten Binary (`ollama --version`)
	live       func(ctx context.Context) string  // Version des laufenden Ollama (/api/version)
	free       func(path string) (uint64, error) // freier Platz auf dem Datentraeger
	http       *http.Client
	verifyWait time.Duration // so lange darf das neue Ollama brauchen, bis /api/version die Zielversion nennt
	verifyPoll time.Duration
}

func newOllamaUpdater(cfg OllamaUpdateCfg, specs []ChildSpec, sup *Supervisor, upstream string, log *Logger) *OllamaUpdater {
	u := &OllamaUpdater{cfg: cfg, sup: sup, upstream: upstream, log: log, http: &http.Client{Timeout: 90 * time.Minute},
		verifyWait: 3 * time.Minute, verifyPoll: 2 * time.Second}
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
	if i := strings.LastIndexAny(cmd, `\/`); i >= 0 { // beide Trenner, unabhaengig von der Plattform des Aufrufers
		b = cmd[i+1:]
	}
	b = strings.ToLower(b)
	return b == "ollama.exe" || b == "ollama"
}

// Managed: laeuft Ollama als Kind dieses Agenten (dann kann der Router ein Update anstossen)?
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

// Handle nimmt einen Auftrag an; ein Auftrag zur Zeit, der Rest passiert im Hintergrund.
func (u *OllamaUpdater) Handle(o OllamaUpdateOrder) {
	u.mu.Lock()
	if u.busy {
		u.mu.Unlock()
		u.log.Warnf("ollama-update: Auftrag auf %s verworfen, es laeuft schon einer", o.Version)
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
		u.log.Infof("ollama-update: Auftrag auf %s (%s, %d MB)", o.Version, o.File, o.Size>>20)
		if err := u.run(o); err != nil {
			u.set("failed", o.Version, err.Error())
			u.log.Warnf("ollama-update: fehlgeschlagen: %v", err)
		}
	}()
}

// ollamaArchiveName: Archiv des Releases fuer dieses System. Linux-Archive sind tar.zst (Zstandard ist nicht in der
// Go-Standardbibliothek) - dort meldet der Agent den Auftrag als nicht unterstuetzt.
func ollamaArchiveName() (string, error) {
	switch runtime.GOOS + "/" + runtime.GOARCH {
	case "windows/amd64":
		return "ollama-windows-amd64.zip", nil
	case "windows/arm64":
		return "ollama-windows-arm64.zip", nil
	}
	return "", fmt.Errorf("Ollama-Update fuer %s/%s noch nicht unterstuetzt (Linux-Archive sind tar.zst)", runtime.GOOS, runtime.GOARCH)
}

// ollamaExeName: Name der Binary im Archiv (Zip = Windows).
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

// fetchChecksum: sha256sum.txt des Releases von der festen Quelle holen und den Eintrag der Datei liefern.
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

// parseChecksum: Zeilen "<hex>  ./<datei>" (GNU-Format, auch "*<datei>" oder ohne Praefix).
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

// download holt das Archiv, prueft Groesse und SHA-256 und meldet den Fortschritt in Zehnerschritten.
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

// extractZip entpackt nach dst; Pfade, die aus dst hinausfuehren, werden abgelehnt (Zip-Slip).
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

// probeOllamaVersion: `ollama --version` der entpackten Binary. OLLAMA_HOST auf einen toten Port, damit sie nicht den
// laufenden Server fragt, sondern ihre eigene Version nennt ("Warning: client version is X").
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

// parseOllamaVersion: "client version is X" hat Vorrang (steht da, sobald Client und erreichbarer Server verschieden sind
// oder kein Server antwortet), sonst "ollama version is X".
func parseOllamaVersion(out string) (string, error) {
	if m := clientVersionRe.FindStringSubmatch(out); m != nil {
		return m[1], nil
	}
	if m := serverVersionRe.FindStringSubmatch(out); m != nil {
		return m[1], nil
	}
	return "", fmt.Errorf("keine Version in %q", strings.TrimSpace(out))
}

// renameRetry: Umbenennen mit Wiederholung - ein Virenscanner oder der gerade beendete Prozess haelt Dateien kurz fest.
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

// swapTop setzt die obersten Eintraege des neuen Ordners (ollama.exe, lib) in dir ein; Vorhandenes wandert nach backup.
// Liefert die eingesetzten Namen (fuer rollbackTop).
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

// rollbackTop macht swapTop rueckgaengig: neue Dateien zurueck nach newDir, Sicherung wieder an ihren Platz.
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
	defer os.RemoveAll(staging) // Erfolg: alte Dateien weg; Fehler: neue Dateien weg (die Sicherung ist dann schon zurueck)
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
	// Nachweis: das laufende Ollama muss die Zielversion melden
	deadline := time.Now().Add(u.verifyWait)
	for time.Now().Before(deadline) {
		time.Sleep(u.verifyPoll)
		if v := u.live(ctx); v == o.Version {
			u.log.Infof("ollama-update: Ollama laeuft mit %s", v)
			return nil
		}
	}
	u.log.Warnf("ollama-update: Ollama meldet sich nicht mit %s - zurueck auf die Sicherung", o.Version)
	u.hold()
	rerr := rollbackTop(u.dir, newDir, backup, moved)
	u.release()
	return fmt.Errorf("neues Ollama meldete sich nicht innerhalb %s mit %s - zurueckgesetzt (%v)", u.verifyWait, o.Version, rerr == nil)
}
