package main

import "testing"

func TestUninstallUpdate(t *testing.T) {
	dir := `C:\Users\u\AppData\Local\Programs\Ollama`
	cases := []struct {
		loc, name, ver, want, wantName string
		change                         bool
	}{
		// Case from 2026-10-01: Installer entry with trailing separator, old version
		{dir + `\`, "Ollama version 0.34.0", "0.34.0", "0.35.0", "Ollama version 0.35.0", true},
		// Different path casing (upper/lower, slashes) counts as the same folder
		{`c:/users/U/AppData/Local/Programs/Ollama/`, "Ollama version 0.34.0", "0.34.0", "0.35.0", "Ollama version 0.35.0", true},
		// Already up to date: do not write
		{dir + `\`, "Ollama version 0.35.0", "0.35.0", "0.35.0", "Ollama version 0.35.0", false},
		// Foreign folder (different installation, different program): do not touch
		{`C:\Program Files\Ollama2`, "Ollama version 0.34.0", "0.34.0", "0.35.0", "", false},
		{"", "Ollama version 0.34.0", "0.34.0", "0.35.0", "", false},
		// Name does not match the installer pattern: only set the version
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
