package main

import (
	"context"
	"strings"
	"time"
)

// Windows-Eintrag der Ollama-Installation nachfuehren (0.16.0). Der Ollama-Installer legt unter "Apps" einen
// Deinstallationseintrag an (DisplayName "Ollama version X", DisplayVersion X); winget und UniGetUI lesen die installierte
// Version nur daraus. Tauscht der Agent die Dateien selbst (ollamaupdate.go), blieb der Eintrag auf der alten Version stehen:
// winget bot dann jedes Release als Update an, und der Installer scheiterte am Ollama, das als Kind des Dienstes laeuft
// (2026-10-01: Eintrag 0.34.0, laufend 0.35.0). Jetzt setzt der Agent den Eintrag nach jedem Tausch und gleicht ihn beim
// Start und stuendlich ab - ein Benutzer-Zweig der Registry ist nur geladen, solange der Benutzer angemeldet ist.

// normDir vergleicht Ordnerpfade unabhaengig von Gross-/Kleinschreibung, Schraegstrich-Art und abschliessendem Trenner.
func normDir(d string) string {
	d = strings.ReplaceAll(d, "/", `\`)
	return strings.ToLower(strings.TrimRight(d, `\`))
}

// uninstallUpdate: neue Werte fuer einen Deinstallationseintrag, wenn er zur Ollama-Installation in dir gehoert und nicht
// version zeigt. DisplayName wird nur angepasst, wenn er dem Muster des Ollama-Installers folgt ("Ollama version X").
func uninstallUpdate(installLocation, displayName, displayVersion, dir, version string) (name, ver string, change bool) {
	if installLocation == "" || dir == "" || normDir(installLocation) != normDir(dir) {
		return "", "", false
	}
	name, ver = displayName, version
	if strings.HasPrefix(displayName, "Ollama version ") {
		name = "Ollama version " + version
	}
	return name, ver, name != displayName || ver != displayVersion
}

// syncRegistration bringt den Windows-Eintrag auf version. Nur wenn Ollama ein Kind dieses Agenten ist: eine Tray-Installation
// aktualisiert sich mit ihrem eigenen Installer, der den Eintrag selbst pflegt.
func (u *OllamaUpdater) syncRegistration(version string) {
	if !u.Managed() || version == "" {
		return
	}
	changed, err := syncUninstallEntries(u.dir, version)
	for _, c := range changed {
		u.log.Infof("ollama-update: Windows-Eintrag %s auf %s gesetzt (Apps, winget)", c, version)
	}
	if err != nil {
		u.log.Warnf("ollama-update: Windows-Eintrag nicht aktualisiert: %v", err)
	}
}

// RegistrationLoop gleicht den Windows-Eintrag 30 s nach dem Start und danach stuendlich mit der laufenden Version ab.
func (u *OllamaUpdater) RegistrationLoop(ctx context.Context) {
	if !u.Managed() {
		return
	}
	wait := 30 * time.Second
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		wait = time.Hour
		if v := u.live(ctx); v != "" {
			u.syncRegistration(v)
		}
	}
}
