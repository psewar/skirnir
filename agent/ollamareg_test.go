package main

import "testing"

func TestUninstallUpdate(t *testing.T) {
	dir := `C:\Users\u\AppData\Local\Programs\Ollama`
	cases := []struct {
		loc, name, ver, want, wantName string
		change                         bool
	}{
		// der Fall vom 2026-10-01: Eintrag des Installers mit Trenner am Ende, alte Version
		{dir + `\`, "Ollama version 0.34.0", "0.34.0", "0.35.0", "Ollama version 0.35.0", true},
		// andere Schreibweise des Pfads (Gross/klein, Schraegstriche) zaehlt als derselbe Ordner
		{`c:/users/U/AppData/Local/Programs/Ollama/`, "Ollama version 0.34.0", "0.34.0", "0.35.0", "Ollama version 0.35.0", true},
		// schon aktuell: nichts schreiben
		{dir + `\`, "Ollama version 0.35.0", "0.35.0", "0.35.0", "Ollama version 0.35.0", false},
		// fremder Ordner (andere Installation, anderes Programm): nicht anfassen
		{`C:\Program Files\Ollama2`, "Ollama version 0.34.0", "0.34.0", "0.35.0", "", false},
		{"", "Ollama version 0.34.0", "0.34.0", "0.35.0", "", false},
		// Name folgt nicht dem Installer-Muster: nur die Version setzen
		{dir, "Ollama", "0.34.0", "0.35.0", "Ollama", true},
	}
	for i, c := range cases {
		name, ver, change := uninstallUpdate(c.loc, c.name, c.ver, dir, c.want)
		if change != c.change || (change && (ver != c.want || name != c.wantName)) {
			t.Errorf("Fall %d: %q %q %v, erwartet %q %q %v", i, name, ver, change, c.wantName, c.want, c.change)
		}
	}
	if _, _, change := uninstallUpdate(dir, "Ollama version 0.34.0", "0.34.0", "", "0.35.0"); change {
		t.Error("ohne bekannten Ollama-Ordner nichts anfassen")
	}
}
