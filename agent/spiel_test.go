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
		{"kein Spiel", []procInfo{{"explorer.exe", `C:\Windows\explorer.exe`}, {"ollama.exe", `C:\Ollama\ollama.exe`}}, "", "", false},
		{"Pfad unter Steam (gross/klein egal)", []procInfo{{"cs2.exe", `d:\steamlibrary\STEAMAPPS\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe`}}, "cs2.exe", "pfad", true},
		{"Ausnahmeordner zaehlt nicht", []procInfo{{"x.exe", `D:\SteamLibrary\steamapps\common\Steamworks Shared\_CommonRedist\x.exe`}}, "", "", false},
		{"Pfad unlesbar -> per Name", []procInfo{{"Diablo IV.exe", ""}}, "Diablo IV.exe", "name", true},
		{"Name gleich, aber Pfad lesbar woanders -> kein Spiel", []procInfo{{"Diablo IV.exe", `C:\Temp\Diablo IV.exe`}}, "", "", false},
		{"Ordnerpraefix allein reicht nicht", []procInfo{{"a.exe", `C:\Program Files (x86)\Diablo IV Tools\a.exe`}}, "", "", false},
	} {
		g, v, ok := matchGame(c.procs, idx)
		if g != c.game || v != c.via || ok != c.ok {
			t.Errorf("%s: matchGame = %q,%q,%v want %q,%q,%v", c.name, g, v, ok, c.game, c.via, c.ok)
		}
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
