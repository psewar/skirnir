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

// Game and main memory as fact (0.17.0). Trigger 2026-10-04: Diablo IV + qwen3.8@196k -> RAM 94 %, free commit 1.8 GB,
// ~900 Hard Faults/s. The router kept the node free because Diablo used little VRAM (no foreign VRAM) and it did not know about
// main memory. Now the agent reports both: the router marks the node busy during a game and
// does not cold-load anything when memory is tight. Detection is via the game watcher (PowerShell, since 2026-09-25):
// process path under a game folder; if the path is unreadable (anti-cheat), match by name of large .exe in the folder.

// GameFact is sent with every heartbeat as block "game". Supported=false: this platform does not detect games (Linux).
type GameFact struct {
	Supported bool   `json:"supported"`
	Running   bool   `json:"running"`
	Name      string `json:"name,omitempty"`  // Programmname, z. B. "Diablo IV.exe"
	Via       string `json:"via,omitempty"`   // "pfad" | "name"
	Since     int64  `json:"since,omitempty"` // Unix seconds since the agent first saw it
	Folders   int    `json:"folders"`         // Game folder in the index (0 = nothing installed, so nothing detected)
	// 0.19.0: GPU demand of the game itself, across all its processes (Performance Counters GPU Process Memory / GPU Engine).
	// The router now only marks a game as busy if it actually needs the GPU: a small game (735 MiB, 2 %)
	// previously locked the whole card. Shared memory counts - a game that gets no VRAM left next to a large model
	// falls back to it (Diablo IV 2026-10-04: little VRAM, little load, RAM 94 %). Missing fields mean nothing
	// was measured (no game, measurement failed, Linux); GPUPct is also missing on the first pass of a game (rate needs two).
	Procs           int      `json:"procs,omitempty"`             // Processes that count as games
	GPUDedicatedMiB *int     `json:"gpu_dedicated_mib,omitempty"` // eigener Grafikspeicher
	GPUSharedMiB    *int     `json:"gpu_shared_mib,omitempty"`    // Shared (main) memory used by the GPU for the game
	GPUPct          *float64 `json:"gpu_pct,omitempty"`           // Highest GPU engine utilization by the game
}

// MemFact is sent as block "memory". If a value is missing (CommitFree on Linux), the agent omits it.
type MemFact struct {
	RAMAvailableGiB float64  `json:"ram_available_gib"`
	RAMTotalGiB     float64  `json:"ram_total_gib"`
	CommitFreeGiB   *float64 `json:"commit_free_gib,omitempty"`
	CommitLimitGiB  *float64 `json:"commit_limit_gib,omitempty"`
}

// procInfo: a running process. Path empty = unreadable (protected process).
type procInfo struct {
	Name string
	Path string
	PID  uint32
}

// gameIndex: Game folders and names of their large programs (lowercase, without .exe) -> full path.
type gameIndex struct {
	folders []string
	exes    map[string]string
}

const (
	gamePoll       = 1 * time.Second // 0.19.0 (previously 5 s): one pass costs ~17 ms for ~500 processes (measured 2026-10-21)
	gameReindex    = 6 * time.Hour
	gameExeMinSize = 20 << 20 // Only large programs: small helpers have generic names (crashpad_handler false positive 2026-09-25)
	gameExeDepth   = 3
)

// Publishers whose install locations count as game folders, and their launchers, which do not count as games.
// Launchers case-insensitive: GOG registers as "GOG GALAXY" (false positive 2026-10-21, GalaxyClient.exe counted as game).
var (
	gamePublisher = regexp.MustCompile(`Blizzard|Electronic Arts|Ubisoft|Epic Games|GOG|Riot|Rockstar|Bethesda|Amazon Games`)
	gameLauncher  = regexp.MustCompile(`(?i)Battle\.net|EA app|Ubisoft Connect|Epic Games Launcher|GOG Galaxy|Riot Client|Rockstar Games Launcher`)
	vdfPath       = regexp.MustCompile(`"path"\s+"([^"]+)"`)
)

// Folders under steamapps\common that are never games (runtime installers, controller configuration).
var gameExcluded = []string{`\steamapps\common\steamworks shared\`, `\steamapps\common\steam controller configs\`}

// Generic names that also run outside games: never evaluate by name.
var gameGenericExe = map[string]bool{
	"launcher": true, "setup": true, "installer": true, "unins000": true, "crashreporter": true, "crashreportclient": true,
	"crashpad_handler": true, "unitycrashhandler64": true, "unitycrashhandler32": true, "dxsetup": true,
	"vc_redist.x64": true, "vc_redist.x86": true, "updater": true, "update": true, "helper": true, "cefprocess": true,
	"cefsharp.browsersubprocess": true, "qtwebengineprocess": true,
}

// parseLibraryVDF: Steam libraries from libraryfolders.vdf, each as <path>\steamapps\common.
func parseLibraryVDF(content string) []string {
	var out []string
	for _, m := range vdfPath.FindAllStringSubmatch(content, -1) {
		p := strings.TrimRight(strings.ReplaceAll(m[1], `\\`, `\`), `\`)
		out = append(out, p+`\steamapps\common`)
	}
	return out
}

// publisherFolder: Is this uninstall entry a game from a known publisher (not a launcher, not from Steam)?
func publisherFolder(publisher, displayName, location string) (string, bool) {
	loc := strings.TrimRight(strings.TrimSpace(location), `\`)
	if loc == "" || !gamePublisher.MatchString(publisher) || gameLauncher.MatchString(displayName) {
		return "", false
	}
	if strings.Contains(strings.ToLower(loc+`\`), `\steamapps\common\`) {
		return "", false // Already in the index via the Steam library
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

// underFolder: Is path (Windows notation) inside one of the folders? Case-insensitive like the filesystem.
func underFolder(path string, folders []string) bool {
	lp := strings.ToLower(path)
	for _, f := range folders {
		if strings.HasPrefix(lp, strings.ToLower(strings.TrimRight(f, `\/`))+`\`) {
			return true
		}
	}
	return false
}

// uniqueFolders: Sorted, deduplicated (case-insensitive), only those where exists is true.
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

// exeIndex: Names of large .exe up to depth gameExeDepth under the folders, excluding exception folders and generic names.
func exeIndex(folders []string) map[string]string {
	out := map[string]string{}
	for _, root := range folders {
		base := strings.Count(filepath.Clean(root), string(filepath.Separator))
		// WalkDir reports unreadable subdirectories to the function; they are skipped, the rest is read.
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

// gameHit: A process that counts as a game, and how it was detected ("path" | "name").
type gameHit struct {
	proc procInfo
	via  string
}

// gameHits: All processes that are games. Path readable -> only path counts; otherwise name against index. All, not
// just the first: some games render in a separate child process, and GPU demand (0.19.0) applies to all together.
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

// matchGame: First process that is a game (name and detection path for the fact).
func matchGame(procs []procInfo, idx gameIndex) (name, via string, ok bool) {
	if h := gameHits(procs, idx); len(h) > 0 {
		return h[0].proc.Name, h[0].via, true
	}
	return "", "", false
}

// gpuUse: GPU demand per PID, as provided by Performance Counters. pct missing for a PID if no rate is available yet.
type gpuUse struct {
	dedicated map[uint32]uint64
	shared    map[uint32]uint64
	pct       map[uint32]float64 // Highest engine utilization of the PID
}

// footprint: GPU demand of game processes combined. Memory summed, utilization = highest value of a process (like the
// GPU column in Task Manager). pctOK=false if no utilization is available for any game process.
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

// GameWatch checks every gamePoll whether a game is running; it rebuilds the index (folder + program name) every gameReindex.
type GameWatch struct {
	log *Logger

	mu    sync.Mutex
	fact  GameFact
	idx   gameIndex
	idxAt time.Time

	// 0.19.0: GPU demand measurement, open only while a game is running (engine utilization is a rate and needs
	// the same query over two passes). nil = not open; newGPUSampler is a stub without measurement on Linux.
	sampler    gpuSampler
	samplerErr string // last error, logged only on change
}

// gpuSampler measures GPU demand of all processes (Windows: PDH, gpufoot_windows.go).
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
		g.log.Infof("game: index %d game folders, %d program names", len(folders), len(idx.exes))
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
			g.log.Infof("game: %s running (via %s)", name, via)
		} else {
			g.log.Infof("game: ended")
		}
	}
}

// measure: GPU demand of game processes; without a game, the measurement is closed (costs nothing). If it fails, fields
// are missing in the fact (router then falls back to "every game is busy") and the error appears once in the log.
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
		g.sampler = nil // next pass opens anew
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
			g.log.Warnf("game: GPU demand not measurable: %s", msg)
		} else if g.samplerErr != "" {
			g.log.Infof("game: GPU demand measurable again")
		}
		g.samplerErr = msg
	}
}

// Fact: current state for Heartbeat and Status.
func (g *GameWatch) Fact() GameFact {
	if g == nil {
		return GameFact{}
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.fact
}

// probeGame: command "game" - print index, hits, and memory once (like spiel-waechter.ps1 -Probe).
func probeGame() {
	if !gameScanSupported {
		fmt.Println("game detection: not supported on this platform")
	} else {
		t0 := time.Now()
		folders := gameFolders()
		idx := gameIndex{folders: folders, exes: exeIndex(folders)}
		fmt.Printf("game folders (%d, index in %.1f s):\n", len(folders), time.Since(t0).Seconds())
		for _, f := range folders {
			fmt.Println("  " + f)
		}
		fmt.Printf("program names in the index: %d\n", len(idx.exes))
		t1 := time.Now()
		procs := listProcesses()
		noPath := 0
		for _, p := range procs {
			if p.Path == "" {
				noPath++
			}
		}
		hits := gameHits(procs, idx)
		fmt.Printf("processes: %d (%d without readable path), check %.0f ms\n", len(procs), noPath, float64(time.Since(t1).Microseconds())/1000)
		if len(hits) > 0 {
			var pids []uint32
			for _, h := range hits {
				pids = append(pids, h.proc.PID)
				fmt.Printf("game detected: %s (pid %d, via %s)\n", h.proc.Name, h.proc.PID, h.via)
			}
			probeFootprint(pids)
		} else {
			fmt.Println("game detected: no")
		}
	}
	if m, ok := memoryFact(); ok {
		b, _ := json.Marshal(m)
		fmt.Printf("memory: %s\n", b)
	} else {
		fmt.Println("memory: not readable")
	}
}

// probeFootprint: GPU demand of game processes as in operation - collected twice (utilization is a rate) and the time
// of one collection, so the cost of the 1s tick is visible.
func probeFootprint(pids []uint32) {
	s, err := newGPUSampler()
	if err != nil {
		fmt.Println("gpu demand: not measurable:", err)
		return
	}
	defer s.close()
	var u gpuUse
	var elapsed time.Duration
	for i := 0; i < 2; i++ {
		if i > 0 {
			time.Sleep(time.Second)
		}
		t := time.Now()
		if u, err = s.sample(); err != nil {
			fmt.Println("gpu demand: not measurable:", err)
			return
		}
		elapsed = time.Since(t)
	}
	d, sh, p, ok := footprint(pids, u)
	pct := "no rate"
	if ok {
		pct = fmt.Sprintf("%.1f %%", p)
	}
	fmt.Printf("gpu demand: dedicated %d MiB, shared %d MiB, engine %s (collecting %.1f ms)\n", d, sh, pct, float64(elapsed.Microseconds())/1000)
}

func gib(bytes uint64) float64 {
	return float64(int64(float64(bytes)/(1<<30)*10+0.5)) / 10
}
