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
}

// gameIndex: Spielordner und Namen ihrer grossen Programme (Kleinbuchstaben, ohne .exe) -> voller Pfad.
type gameIndex struct {
	folders []string
	exes    map[string]string
}

const (
	gamePoll       = 5 * time.Second
	gameReindex    = 6 * time.Hour
	gameExeMinSize = 20 << 20 // nur grosse Programme: kleine Helfer tragen Allerweltsnamen (crashpad_handler-Fehlalarm 2026-09-25)
	gameExeDepth   = 3
)

// Verlage, deren Installationsorte als Spielordner gelten, und ihre Launcher, die nicht als Spiel zaehlen.
var (
	gamePublisher = regexp.MustCompile(`Blizzard|Electronic Arts|Ubisoft|Epic Games|GOG|Riot|Rockstar|Bethesda|Amazon Games`)
	gameLauncher  = regexp.MustCompile(`Battle\.net|EA app|Ubisoft Connect|Epic Games Launcher|GOG Galaxy|Riot Client|Rockstar Games Launcher`)
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

// matchGame: erster Prozess, der ein Spiel ist. Pfad lesbar -> nur der Pfad zaehlt; sonst der Name gegen den Index.
func matchGame(procs []procInfo, idx gameIndex) (name, via string, ok bool) {
	for _, p := range procs {
		if p.Path != "" {
			if !gameExcludedPath(p.Path) && underFolder(p.Path, idx.folders) {
				return p.Name, "pfad", true
			}
			continue
		}
		n := strings.ToLower(strings.TrimSuffix(p.Name, filepath.Ext(p.Name)))
		if _, hit := idx.exes[n]; hit {
			return p.Name, "name", true
		}
	}
	return "", "", false
}

// GameWatch prueft alle gamePoll, ob ein Spiel laeuft; den Index (Ordner + Programmnamen) baut es alle gameReindex neu.
type GameWatch struct {
	log *Logger

	mu    sync.Mutex
	fact  GameFact
	idx   gameIndex
	idxAt time.Time
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
	name, via, running := matchGame(listProcesses(), g.idx)
	g.mu.Lock()
	was := g.fact.Running
	g.fact.Running, g.fact.Name, g.fact.Via = running, name, via
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
		name, via, ok := matchGame(procs, idx)
		fmt.Printf("prozesse: %d (%d ohne lesbaren Pfad), Pruefung %.0f ms\n", len(procs), ohne, float64(time.Since(t1).Microseconds())/1000)
		if ok {
			fmt.Printf("spiel erkannt: %s (per %s)\n", name, via)
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

func gib(bytes uint64) float64 {
	return float64(int64(float64(bytes)/(1<<30)*10+0.5)) / 10
}
