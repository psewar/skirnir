package main

import (
	"context"
	"strings"
	"time"
)

// Track the Windows registry entry for the Ollama installation (0.16.0). The Ollama installer creates an
// uninstall entry under "Apps" (DisplayName "Ollama version X", DisplayVersion X); winget and UniGetUI read the installed
// version only from there. If the agent swaps the files itself (ollamaupdate.go), the entry remained on the old version:
// winget then offered every release as an update, and the installer failed on Ollama running as a child of the service
// (2026-10-01: entry 0.34.0, running 0.35.0). Now the agent updates the entry after each swap and reconciles it on
// startup and hourly - a user registry hive is only loaded while the user is logged in.

// normDir compares directory paths ignoring case, slash type, and trailing separators.
func normDir(d string) string {
	d = strings.ReplaceAll(d, "/", `\`)
	return strings.ToLower(strings.TrimRight(d, `\`))
}

// uninstallUpdate: new values for an uninstall entry if it belongs to the Ollama installation in dir and does not
// show version. DisplayName is only adjusted if it follows the Ollama installer pattern ("Ollama version X").
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

// syncRegistration brings the Windows entry to version. Only if Ollama is a child of this agent: a tray installation
// updates itself with its own installer, which maintains the entry.
func (u *OllamaUpdater) syncRegistration(version string) {
	if !u.Managed() || version == "" {
		return
	}
	changed, err := syncUninstallEntries(u.dir, version)
	for _, c := range changed {
		u.log.Infof("ollama-update: Windows entry %s set to %s (Apps, winget)", c, version)
	}
	if err != nil {
		u.log.Warnf("ollama-update: Windows entry not updated: %v", err)
	}
}

// RegistrationLoop reconciles the Windows entry 30 s after startup and then hourly with the running version.
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
