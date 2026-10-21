package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

// Spiel und Hauptspeicher als Fakt (0.17.0). Anlass 2026-10-04: Diablo IV + qwen3.8@196k -> RAM 94 %, freier Commit 1,8 GB,
// ~900 Hard Faults/s. Der Router hielt den Knoten fuer frei, weil Diablo kaum VRAM belegte (kein fremdes VRAM) und er den
// Hauptspeicher nicht kannte. Jetzt meldet der Agent beides selbst: der Router macht den Knoten bei einem Spiel busy und
// laedt bei knappem Speicher nichts kalt nach. Die Erkennung ist die des Spiel-Waechters (PowerShell, seit 2026-09-25):
// Prozesspfad unter einem Spielordner; ist der Pfad nicht lesbar (Anti-Cheat), ueber den Namen grosser .exe im Ordner.

// GameFact geht mit jedem Heartbeat als Block "game". Supported=false: diese Plattform erkennt keine Spiele (Linux).
type GameFact struct {
	Supported bool   `json:"supported"`
	Running   bool   `json:"running"`
	Name      string `json:"name,omitempty"`  // Programmname, z. B. "Diablo IV.exe"
	Via       string `json:"via,omitempty"`   // "pfad" | "name"
	Since     int64  `json:"since,omitempty"` // Unix-Sekunden, seit wann der Agent es sieht
	Folders   int    `json:"folders"`         // Spielordner im Index (0 = nichts installiert, dann erkennt er auch nichts)
	// 0.19.0: GPU-Bedarf des Spiels selbst, ueber alle seine Prozesse (Leistungsindikatoren GPU Process Memory / GPU Engine).
	// Der Router wertet ein Spiel damit nur noch als busy, wenn es die GPU wirklich braucht: ein kleines Spiel (735 MiB, 2 %)
	// sperrte vorher die ganze Karte. Geteilter Speicher zaehlt mit - ein Spiel, das neben einem grossen Modell kein VRAM mehr
	// bekommt, weicht dorthin aus (Diablo IV 2026-10-04: kaum VRAM, kaum Last, RAM 94 %). Fehlen die Felder, ist nichts
	// gemessen (kein Spiel, Messung gescheitert, Linux); GPUPct fehlt auch beim ersten Durchlauf eines Spiels (Rate braucht zwei).
	Procs           int      `json:"procs,omitempty"`             // Prozesse, die als Spiel zaehlen
	GPUDedicatedMiB *int     `json:"gpu_dedicated_mib,omitempty"` // eigener Grafikspeicher
	GPUSharedMiB    *int     `json:"gpu_shared_mib,omitempty"`    // geteilter (Haupt-)Speicher, den die GPU fuer das Spiel nutzt
	GPUPct          *float64 `json:"gpu_pct,omitempty"`           // hoechste Auslastung einer GPU-Engine durch das Spiel
}

// MemFact geht als Block "memory". Fehlt ein Wert (CommitFree auf Linux), laesst der Agent ihn weg.
type MemFact struct {
	RAMAvailableGiB float64  `json:"ram_available_gib"`
	RAMTotalGiB     float64  `json:"ram_total_gib"`
	CommitFreeGiB   *float64 `json:"commit_free_gib,omitempty"`
	CommitLimitGiB  *float64 `json:"commit_limit_gib,omitempty"`
}

// procInfo: ein laufender Prozess. Path leer = nicht lesbar (geschuetzter Prozess).
type procInfo struct {
	Name string
	Path string
	PID  uint32
}

// gameIndex: Spielordner und Namen ihrer grossen Programme (Kleinbuchstaben, ohne .exe) -> voller Pfad.
type gameIndex struct {
	folders []string
	exes    map[string]string
}

const (
	gamePoll       = 1 * time.Second // 0.19.0 (vorher 5 s): ein Durchlauf kostet ~17 ms bei ~500 Prozessen (gemessen 2026-10-21)
	gameReindex    = 6 * time.Hour
	gameExeMinSize = 20 << 20 // nur grosse Programme: kleine Helfer tragen Allerweltsnamen (crashpad_handler-Fehlalarm 2026-09-25)
	gameExeDepth   = 3
)

// Verlage, deren Installationsorte als Spielordner gelten, und ihre Launcher, die nicht als Spiel zaehlen.
// Launcher ohne Gross-/Kleinschreibung: GOG traegt sich als "GOG GALAXY" ein (Fehlalarm 2026-10-21, GalaxyClient.exe galt als Spiel).
var (
	gamePublisher = regexp.MustCompile(`Blizzard|Electronic Arts|Ubisoft|Epic Games|GOG|Riot|Rockstar|Bethesda|Amazon Games`)
	gameLauncher  = regexp.MustCompile(`(?i)Battle\.net|EA app|Ubisoft Connect|Epic Games Launcher|GOG Galaxy|Riot Client|Rockstar Games Launcher`)
	vdfPath       = regexp.MustCompile(`"path"\s+"([^"]+)"`)
)

// Ordner unter steamapps\common, die nie ein Spiel sind (Laufzeit-Installer, Controller-Konfiguration).
var gameExcluded = []string{`\steamapps\common\steamworks shared\`, `\steamapps\common\steam controller configs\`}

// Allerweltsnamen, die auch ausserhalb von Spielen laufen: nie per Namen werten.
var gameGenericExe = map[string]bool{
	"launcher": true, "setup": true, "installer": true, "unins000": true, "crashreporter": true, "crashreportclient": true,
	"crashpad_handler": true, "unitycrashhandler64": true, "unitycrashhandler32": true, "dxsetup": true,
	"vc_redist.x64": true, "vc_redist.x86": true, "updater": true, "update": true, "helper": true, "cefprocess": true,
	"cefsharp.browsersubprocess": true, "qtwebengineprocess": true,
}

// parseLibraryVDF: Steam-Bibliotheken aus libraryfolders.vdf, je als <pfad>\steamapps\common.
func parseLibraryVDF(content string) []string {
	var out []string
	for _, m := range vdfPath.FindAllStringSubmatch(content, -1) {
		p := strings.TrimRight(strings.ReplaceAll(m[1], `\\`, `\`), `\`)
		out = append(out, p+`\steamapps\common`)
	}
	return out
}

// publisherFolder: ist dieser Deinstallationseintrag ein Spiel eines bekannten Verlags (kein Launcher, nicht von Steam)?
func publisherFolder(publisher, displayName, location string) (string, bool) {
	loc := strings.TrimRight(strings.TrimSpace(location), `\`)
	if loc == "" || !gamePublisher.MatchString(publisher) || gameLauncher.MatchString(displayName) {
		return "", false
	}
	if strings.Contains(strings.ToLower(loc+`\`), `\steamapps\common\`) {
		return "", false // steht schon ueber die Steam-Bibliothek im Index
	}
	return loc, true
}

func gameExcludedPath(p string) bool {
	lp := strings.ToLower(p)
	for _, e := range gameExcluded {
		if strings.Contains(lp, e) {
			return true
		}
	}
	return false
}

// underFolder: liegt path (Windows-Schreibweise) in einem der Ordner? Gross/klein egal wie im Dateisystem.
func underFolder(path string, folders []string) bool {
	lp := strings.ToLower(path)
	for _, f := range folders {
		if strings.HasPrefix(lp, strings.ToLower(strings.TrimRight(f, `\/`))+`\`) {
			return true
		}
	}
	return false
}

// uniqueFolders: sortiert, ohne Doppelte (gross/klein egal), nur was exists bejaht.
func uniqueFolders(in []string, exists func(string) bool) []string {
	seen := map[string]bool{}
	var out []string
	for _, f := range in {
		k := strings.ToLower(f)
		if f == "" || seen[k] || !exists(f) {
			continue
		}
		seen[k] = true
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

// exeIndex: Namen grosser .exe bis Tiefe gameExeDepth unter den Ordnern, ohne Ausnahmeordner und Allerweltsnamen.
func exeIndex(folders []string) map[string]string {
	out := map[string]string{}
	for _, root := range folders {
		base := strings.Count(filepath.Clean(root), string(filepath.Separator))
		// WalkDir meldet nicht lesbare Unterordner an die Funktion; die werden uebersprungen, der Rest weiter gelesen.
		_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				if d != nil && d.IsDir() {
					return fs.SkipDir
				}
				return nil
			}
			if d.IsDir() {
				if strings.Count(filepath.Clean(p), string(filepath.Separator))-base > gameExeDepth {
					return fs.SkipDir
				}
				return nil
			}
			if !strings.EqualFold(filepath.Ext(p), ".exe") || gameExcludedPath(p) {
				return nil
			}
			info, ierr := d.Info()
			if ierr != nil || info.Size() < gameExeMinSize {
				return nil
			}
			n := strings.ToLower(strings.TrimSuffix(d.Name(), filepath.Ext(d.Name())))
			if !gameGenericExe[n] {
				out[n] = p
			}
			return nil
		})
	}
	return out
}

// gameHit: ein Prozess, der als Spiel zaehlt, und woran er erkannt wurde ("pfad" | "name").
type gameHit struct {
	proc procInfo
	via  string
}

// gameHits: alle Prozesse, die Spiele sind. Pfad lesbar -> nur der Pfad zaehlt; sonst der Name gegen den Index. Alle, nicht
// nur der erste: manche Spiele zeichnen in einem eigenen Kindprozess, und der GPU-Bedarf (0.19.0) gilt fuer alle zusammen.
func gameHits(procs []procInfo, idx gameIndex) []gameHit {
	var out []gameHit
	for _, p := range procs {
		if p.Path != "" {
			if !gameExcludedPath(p.Path) && underFolder(p.Path, idx.folders) {
				out = append(out, gameHit{p, "pfad"})
			}
			continue
		}
		n := strings.ToLower(strings.TrimSuffix(p.Name, filepath.Ext(p.Name)))
		if _, hit := idx.exes[n]; hit {
			out = append(out, gameHit{p, "name"})
		}
	}
	return out
}

// matchGame: erster Prozess, der ein Spiel ist (Name und Erkennungsweg fuer den Fakt).
func matchGame(procs []procInfo, idx gameIndex) (name, via string, ok bool) {
	if h := gameHits(procs, idx); len(h) > 0 {
		return h[0].proc.Name, h[0].via, true
	}
	return "", "", false
}

// gpuUse: GPU-Bedarf je PID, wie ihn die Leistungsindikatoren liefern. pct fehlt fuer eine PID, wenn noch keine Rate da ist.
type gpuUse struct {
	dedicated map[uint32]uint64
	shared    map[uint32]uint64
	pct       map[uint32]float64 // hoechste Engine-Auslastung der PID
}

// footprint: GPU-Bedarf der Spielprozesse zusammen. Speicher summiert, Auslastung = hoechster Wert eines Prozesses (wie die
// GPU-Spalte im Task-Manager). pctOK=false, wenn fuer keinen Spielprozess eine Auslastung vorliegt.
func footprint(pids []uint32, u gpuUse) (dedMiB, sharedMiB int, pct float64, pctOK bool) {
	var ded, sh uint64
	for _, pid := range pids {
		ded += u.dedicated[pid]
		sh += u.shared[pid]
		if v, ok := u.pct[pid]; ok {
			if !pctOK || v > pct {
				pct = v
			}
			pctOK = true
		}
	}
	return int(ded >> 20), int(sh >> 20), pct, pctOK
}

// GameWatch prueft alle gamePoll, ob ein Spiel laeuft; den Index (Ordner + Programmnamen) baut es alle gameReindex neu.
type GameWatch struct {
	log *Logger

	mu    sync.Mutex
	fact  GameFact
	idx   gameIndex
	idxAt time.Time

	// 0.19.0: Messung des GPU-Bedarfs, offen nur waehrend ein Spiel laeuft (die Engine-Auslastung ist eine Rate und braucht
	// dieselbe Abfrage ueber zwei Durchlaeufe). nil = keine offen; newGPUSampler ist auf Linux ein Stub ohne Messung.
	sampler    gpuSampler
	samplerErr string // letzter Fehler, nur bei Wechsel geloggt
}

// gpuSampler misst den GPU-Bedarf aller Prozesse (Windows: PDH, gpufoot_windows.go).
type gpuSampler interface {
	sample() (gpuUse, error)
	close()
}

func newGameWatch(log *Logger) *GameWatch {
	return &GameWatch{log: log, fact: GameFact{Supported: gameScanSupported}}
}

func (g *GameWatch) Run(ctx context.Context) {
	if !gameScanSupported {
		return
	}
	t := time.NewTicker(gamePoll)
	defer t.Stop()
	for {
		g.poll()
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (g *GameWatch) poll() {
	if time.Since(g.idxAt) >= gameReindex {
		folders := gameFolders()
		idx := gameIndex{folders: folders, exes: exeIndex(folders)}
		g.mu.Lock()
		g.idx, g.idxAt = idx, time.Now()
		g.fact.Folders = len(folders)
		g.mu.Unlock()
		g.log.Infof("spiel: Index %d Spielordner, %d Programmnamen", len(folders), len(idx.exes))
	}
	hits := gameHits(listProcesses(), g.idx)
	running := len(hits) > 0
	var name, via string
	var pids []uint32
	for i, h := range hits {
		if i == 0 {
			name, via = h.proc.Name, h.via
		}
		pids = append(pids, h.proc.PID)
	}
	ded, sh, pct := g.measure(running, pids)
	g.mu.Lock()
	was := g.fact.Running
	g.fact.Running, g.fact.Name, g.fact.Via = running, name, via
	g.fact.Procs, g.fact.GPUDedicatedMiB, g.fact.GPUSharedMiB, g.fact.GPUPct = len(pids), ded, sh, pct
	if running && !was {
		g.fact.Since = time.Now().Unix()
	} else if !running {
		g.fact.Since = 0
	}
	g.mu.Unlock()
	if running != was {
		if running {
			g.log.Infof("spiel: %s laeuft (per %s)", name, via)
		} else {
			g.log.Infof("spiel: beendet")
		}
	}
}

// measure: GPU-Bedarf der Spielprozesse; ohne Spiel wird die Messung geschlossen (kostet dann nichts). Scheitert sie, fehlen
// die Felder im Fakt (der Router faellt dann auf "jedes Spiel ist busy" zurueck) und der Fehler steht einmal im Log.
func (g *GameWatch) measure(running bool, pids []uint32) (ded, sh *int, pct *float64) {
	if !running {
		if g.sampler != nil {
			g.sampler.close()
			g.sampler = nil
		}
		return nil, nil, nil
	}
	if g.sampler == nil {
		s, err := newGPUSampler()
		if err != nil {
			g.noteSampleErr(err)
			return nil, nil, nil
		}
		g.sampler = s
	}
	u, err := g.sampler.sample()
	if err != nil {
		g.noteSampleErr(err)
		g.sampler.close()
		g.sampler = nil // naechster Durchlauf oeffnet neu
		return nil, nil, nil
	}
	g.noteSampleErr(nil)
	d, s, p, ok := footprint(pids, u)
	ded, sh = &d, &s
	if ok {
		pct = &p
	}
	return ded, sh, pct
}

func (g *GameWatch) noteSampleErr(err error) {
	msg := ""
	if err != nil {
		msg = err.Error()
	}
	if msg != g.samplerErr {
		if msg != "" {
			g.log.Warnf("spiel: GPU-Bedarf nicht messbar: %s", msg)
		} else if g.samplerErr != "" {
			g.log.Infof("spiel: GPU-Bedarf wieder messbar")
		}
		g.samplerErr = msg
	}
}

// Fact: der aktuelle Stand fuer Heartbeat und Status.
func (g *GameWatch) Fact() GameFact {
	if g == nil {
		return GameFact{}
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.fact
}

// probeGame: Befehl "spiel" - Index, Treffer und Speicher einmal ausgeben (wie spiel-waechter.ps1 -Probe).
func probeGame() {
	if !gameScanSupported {
		fmt.Println("spielerkennung: auf dieser Plattform nicht unterstuetzt")
	} else {
		t0 := time.Now()
		folders := gameFolders()
		idx := gameIndex{folders: folders, exes: exeIndex(folders)}
		fmt.Printf("spielordner (%d, Index in %.1f s):\n", len(folders), time.Since(t0).Seconds())
		for _, f := range folders {
			fmt.Println("  " + f)
		}
		fmt.Printf("programmnamen im index: %d\n", len(idx.exes))
		t1 := time.Now()
		procs := listProcesses()
		ohne := 0
		for _, p := range procs {
			if p.Path == "" {
				ohne++
			}
		}
		hits := gameHits(procs, idx)
		fmt.Printf("prozesse: %d (%d ohne lesbaren Pfad), Pruefung %.0f ms\n", len(procs), ohne, float64(time.Since(t1).Microseconds())/1000)
		if len(hits) > 0 {
			var pids []uint32
			for _, h := range hits {
				pids = append(pids, h.proc.PID)
				fmt.Printf("spiel erkannt: %s (pid %d, per %s)\n", h.proc.Name, h.proc.PID, h.via)
			}
			probeFootprint(pids)
		} else {
			fmt.Println("spiel erkannt: nein")
		}
	}
	if m, ok := memoryFact(); ok {
		b, _ := json.Marshal(m)
		fmt.Printf("speicher: %s\n", b)
	} else {
		fmt.Println("speicher: nicht lesbar")
	}
}

// probeFootprint: GPU-Bedarf der Spielprozesse wie im Betrieb - zweimal gesammelt (die Auslastung ist eine Rate) und die Zeit
// eines Sammelns, damit der Preis des 1-s-Takts sichtbar ist.
func probeFootprint(pids []uint32) {
	s, err := newGPUSampler()
	if err != nil {
		fmt.Println("gpu-bedarf: nicht messbar:", err)
		return
	}
	defer s.close()
	var u gpuUse
	var dauer time.Duration
	for i := 0; i < 2; i++ {
		if i > 0 {
			time.Sleep(time.Second)
		}
		t := time.Now()
		if u, err = s.sample(); err != nil {
			fmt.Println("gpu-bedarf: nicht messbar:", err)
			return
		}
		dauer = time.Since(t)
	}
	d, sh, p, ok := footprint(pids, u)
	pct := "keine Rate"
	if ok {
		pct = fmt.Sprintf("%.1f %%", p)
	}
	fmt.Printf("gpu-bedarf: eigen %d MiB, geteilt %d MiB, Engine %s (Sammeln %.1f ms)\n", d, sh, pct, float64(dauer.Microseconds())/1000)
}

func gib(bytes uint64) float64 {
	return float64(int64(float64(bytes)/(1<<30)*10+0.5)) / 10
}
