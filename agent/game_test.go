package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseLibraryVDF(t *testing.T) {
	vdf := `"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
	}
	"1"
	{
		"path"		"D:\\SteamLibrary\\"
	}
}`
	got := parseLibraryVDF(vdf)
	want := []string{`C:\Program Files (x86)\Steam\steamapps\common`, `D:\SteamLibrary\steamapps\common`}
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("parseLibraryVDF = %q, want %q", got, want)
	}
}

func TestPublisherFolder(t *testing.T) {
	cases := []struct {
		pub, name, loc string
		want           string
		ok             bool
	}{
		{"Blizzard Entertainment", "Diablo IV", `C:\Program Files (x86)\Diablo IV\`, `C:\Program Files (x86)\Diablo IV`, true},
		{"Blizzard Entertainment", "Battle.net", `C:\Program Files (x86)\Battle.net`, "", false}, // Launcher
		{"GOG.com", "GOG GALAXY", `C:\Program Files\GOG Galaxy\`, "", false},                     // Launcher, Schreibweise wie in der Registry
		{"Electronic Arts", "EA app", `C:\Program Files\Electronic Arts\EA Desktop`, "", false},  // Launcher
		{"Valve", "Counter-Strike 2", `D:\SteamLibrary\steamapps\common\CS2`, "", false},         // kein Verlag der Liste
		{"Ubisoft", "Anno 1800", `D:\SteamLibrary\steamapps\common\Anno 1800`, "", false},        // schon ueber Steam
		{"Bethesda Softworks", "Starfield", "", "", false},                                       // ohne Ort
		{"Microsoft Corporation", "Office", `C:\Program Files\Microsoft Office`, "", false},
	}
	for _, c := range cases {
		got, ok := publisherFolder(c.pub, c.name, c.loc)
		if ok != c.ok || got != c.want {
			t.Errorf("publisherFolder(%q, %q, %q) = %q,%v want %q,%v", c.pub, c.name, c.loc, got, ok, c.want, c.ok)
		}
	}
}

func TestMatchGame(t *testing.T) {
	idx := gameIndex{
		folders: []string{`D:\SteamLibrary\steamapps\common`, `C:\Program Files (x86)\Diablo IV`},
		exes:    map[string]string{"diablo iv": `C:\Program Files (x86)\Diablo IV\Diablo IV.exe`},
	}
	type tc struct {
		name  string
		procs []procInfo
		game  string
		via   string
		ok    bool
	}
	for _, c := range []tc{
		{"kein Spiel", []procInfo{{Name: "explorer.exe", Path: `C:\Windows\explorer.exe`}, {Name: "ollama.exe", Path: `C:\Ollama\ollama.exe`}}, "", "", false},
		{"Pfad unter Steam (gross/klein egal)", []procInfo{{Name: "cs2.exe", Path: `d:\steamlibrary\STEAMAPPS\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe`}}, "cs2.exe", "pfad", true},
		{"Ausnahmeordner zaehlt nicht", []procInfo{{Name: "x.exe", Path: `D:\SteamLibrary\steamapps\common\Steamworks Shared\_CommonRedist\x.exe`}}, "", "", false},
		{"Pfad unlesbar -> per Name", []procInfo{{Name: "Diablo IV.exe", Path: ""}}, "Diablo IV.exe", "name", true},
		{"Name gleich, aber Pfad lesbar woanders -> kein Spiel", []procInfo{{Name: "Diablo IV.exe", Path: `C:\Temp\Diablo IV.exe`}}, "", "", false},
		{"Ordnerpraefix allein reicht nicht", []procInfo{{Name: "a.exe", Path: `C:\Program Files (x86)\Diablo IV Tools\a.exe`}}, "", "", false},
	} {
		g, v, ok := matchGame(c.procs, idx)
		if g != c.game || v != c.via || ok != c.ok {
			t.Errorf("%s: matchGame = %q,%q,%v want %q,%q,%v", c.name, g, v, ok, c.game, c.via, c.ok)
		}
	}
}

// GOG legt Spiele UNTER dem Launcher-Ordner ab (Registry-Eintraege in dieser Form). Der Launcher darf nicht in den Index,
// seine Spiele schon - sonst galt GalaxyClient.exe als Spiel (2026-10-21) oder ein GOG-Spiel als keins.
func TestGOGLauncherAndGame(t *testing.T) {
	var idx gameIndex
	for _, e := range [][3]string{
		{"GOG.com", "GOG GALAXY", `C:\Program Files\GOG Galaxy\`},
		{"GOG.com", "Example Game", `C:\Program Files\GOG Galaxy\Games\Example Game\`},
	} {
		if dir, ok := publisherFolder(e[0], e[1], e[2]); ok {
			idx.folders = append(idx.folders, dir)
		}
	}
	if len(idx.folders) != 1 || idx.folders[0] != `C:\Program Files\GOG Galaxy\Games\Example Game` {
		t.Fatalf("Index-Ordner = %q, want nur den Spielordner", idx.folders)
	}
	if g, _, ok := matchGame([]procInfo{{Name: "GalaxyClient.exe", Path: `C:\Program Files\GOG Galaxy\GalaxyClient.exe`}}, idx); ok {
		t.Errorf("Launcher gilt als Spiel: %q", g)
	}
	if g, v, ok := matchGame([]procInfo{{Name: "ExampleGame.exe", Path: `C:\Program Files\GOG Galaxy\Games\Example Game\ExampleGame.exe`}}, idx); !ok || v != "pfad" {
		t.Errorf("GOG-Spiel nicht erkannt: %q,%q,%v", g, v, ok)
	}
}

// 0.19.0: alle Prozesse eines Spiels zaehlen fuer den GPU-Bedarf (manche Spiele zeichnen in einem Kindprozess).
func TestGameHitsAllProcesses(t *testing.T) {
	idx := gameIndex{folders: []string{`D:\SteamLibrary\steamapps\common`}, exes: map[string]string{"anticheat game": `x`}}
	hits := gameHits([]procInfo{
		{Name: "launcher.exe", Path: `D:\SteamLibrary\steamapps\common\Example\launcher.exe`, PID: 10},
		{Name: "explorer.exe", Path: `C:\Windows\explorer.exe`, PID: 11},
		{Name: "render.exe", Path: `D:\SteamLibrary\steamapps\common\Example\bin\render.exe`, PID: 12},
		{Name: "AntiCheat Game.exe", Path: "", PID: 13},
	}, idx)
	var pids []uint32
	for _, h := range hits {
		pids = append(pids, h.proc.PID)
	}
	if len(pids) != 3 || pids[0] != 10 || pids[1] != 12 || pids[2] != 13 || hits[2].via != "name" {
		t.Errorf("gameHits PIDs = %v, want [10 12 13] (letzter per Name)", pids)
	}
}

func TestFootprint(t *testing.T) {
	u := gpuUse{
		dedicated: map[uint32]uint64{10: 512 << 20, 12: 1024 << 20, 99: 8 << 30},
		shared:    map[uint32]uint64{12: 2048 << 20},
		pct:       map[uint32]float64{10: 3.5, 12: 41.0, 99: 100},
	}
	d, s, p, ok := footprint([]uint32{10, 12}, u)
	if d != 1536 || s != 2048 || !ok || p != 41.0 {
		t.Errorf("footprint = %d MiB eigen, %d MiB geteilt, %.1f %% (%v); want 1536, 2048, 41.0 - fremde PID 99 zaehlt nicht", d, s, p, ok)
	}
	// erster Durchlauf: noch keine Rate -> Speicher ja, Auslastung fehlt (Router wertet dann nur den Speicher)
	_, _, _, ok = footprint([]uint32{10}, gpuUse{dedicated: u.dedicated, shared: u.shared, pct: map[uint32]float64{}})
	if ok {
		t.Errorf("ohne Rate darf keine Auslastung gemeldet werden")
	}
}

func TestExeIndex(t *testing.T) {
	root := t.TempDir()
	mk := func(rel string, size int64) {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		f, err := os.Create(p)
		if err != nil {
			t.Fatal(err)
		}
		if err := f.Truncate(size); err != nil { // duenn besetzt: belegt keinen Platz
			t.Fatal(err)
		}
		f.Close()
	}
	mk("Spiel/Spiel.exe", 30<<20)
	mk("Spiel/bin/x64/Tief.exe", 30<<20)   // Tiefe 3: drin
	mk("Spiel/a/b/c/d/ZuTief.exe", 30<<20) // Tiefe 5: draussen
	mk("Spiel/Klein.exe", 1<<20)           // zu klein
	mk("Spiel/Launcher.exe", 30<<20)       // Allerweltsname
	mk("Spiel/daten.pak", 30<<20)          // keine .exe
	got := exeIndex([]string{root})
	for _, n := range []string{"spiel", "tief"} {
		if _, ok := got[n]; !ok {
			t.Errorf("%s fehlt im Index: %v", n, got)
		}
	}
	for _, n := range []string{"zutief", "klein", "launcher", "daten"} {
		if _, ok := got[n]; ok {
			t.Errorf("%s darf nicht im Index stehen: %v", n, got)
		}
	}
}

func TestUniqueFolders(t *testing.T) {
	got := uniqueFolders([]string{`D:\B`, `d:\b`, "", `C:\A`, `X:\weg`}, func(p string) bool { return p != `X:\weg` })
	if len(got) != 2 || got[0] != `C:\A` || got[1] != `D:\B` {
		t.Fatalf("uniqueFolders = %q", got)
	}
}

func TestGiB(t *testing.T) {
	if g := gib(1932735283); g != 1.8 { // 1,8 GiB
		t.Fatalf("gib = %v", g)
	}
}
