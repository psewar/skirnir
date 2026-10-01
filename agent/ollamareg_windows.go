//go:build windows

package main

import (
	"errors"
	"fmt"
	"strings"

	"golang.org/x/sys/windows/registry"
)

const uninstallKey = `Software\Microsoft\Windows\CurrentVersion\Uninstall`

// syncUninstallEntries setzt alle Deinstallationseintraege, deren InstallLocation der Ollama-Ordner dir ist, auf version.
// Durchsucht HKLM und jeden geladenen Benutzer-Zweig unter HKEY_USERS (der Agent laeuft als LocalSystem, die Pro-Benutzer-
// Installation steht im Zweig des Benutzers). Geschrieben wird nur ein Eintrag, der wirklich veraltet ist.
func syncUninstallEntries(dir, version string) ([]string, error) {
	type root struct {
		label string
		key   registry.Key
		path  string
	}
	roots := []root{{"HKLM", registry.LOCAL_MACHINE, uninstallKey}}
	if sids, err := registry.USERS.ReadSubKeyNames(-1); err == nil {
		for _, sid := range sids {
			if strings.HasSuffix(sid, "_Classes") {
				continue
			}
			roots = append(roots, root{`HKU\` + sid, registry.USERS, sid + `\` + uninstallKey})
		}
	}
	var changed []string
	var errs []error
	for _, r := range roots {
		k, err := registry.OpenKey(r.key, r.path, registry.ENUMERATE_SUB_KEYS)
		if err != nil {
			continue // Zweig ohne Uninstall-Schluessel (Dienstkonten, Standardprofil): nichts zu tun
		}
		names, _ := k.ReadSubKeyNames(-1)
		k.Close()
		for _, n := range names {
			sub := r.path + `\` + n
			e, err := registry.OpenKey(r.key, sub, registry.QUERY_VALUE)
			if err != nil {
				continue
			}
			loc, _, _ := e.GetStringValue("InstallLocation")
			name, _, _ := e.GetStringValue("DisplayName")
			ver, _, _ := e.GetStringValue("DisplayVersion")
			e.Close()
			newName, newVer, change := uninstallUpdate(loc, name, ver, dir, version)
			if !change {
				continue
			}
			w, err := registry.OpenKey(r.key, sub, registry.SET_VALUE)
			if err != nil {
				errs = append(errs, fmt.Errorf(`%s\...\%s: %w`, r.label, n, err))
				continue
			}
			err = errors.Join(w.SetStringValue("DisplayVersion", newVer), w.SetStringValue("DisplayName", newName))
			w.Close()
			if err != nil {
				errs = append(errs, fmt.Errorf(`%s\...\%s: %w`, r.label, n, err))
				continue
			}
			changed = append(changed, r.label+`\...\`+n)
		}
	}
	return changed, errors.Join(errs...)
}
