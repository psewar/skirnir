//go:build windows

package main

import (
	"os"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

const gameScanSupported = true

var procGlobalMemoryStatusEx = windows.NewLazySystemDLL("kernel32.dll").NewProc("GlobalMemoryStatusEx")

// MEMORYSTATUSEX
type memoryStatusEx struct {
	Length               uint32
	MemoryLoad           uint32
	TotalPhys            uint64
	AvailPhys            uint64
	TotalPageFile        uint64
	AvailPageFile        uint64
	TotalVirtual         uint64
	AvailVirtual         uint64
	AvailExtendedVirtual uint64
}

// memoryFact: verfuegbarer RAM (inkl. Standby) und freier Commit - dieselben Zahlen wie FreePhysicalMemory /
// FreeVirtualMemory des Spiel-Waechters. TotalPageFile ist das Commit-Limit (RAM + Auslagerungsdatei).
func memoryFact() (MemFact, bool) {
	var m memoryStatusEx
	m.Length = uint32(unsafe.Sizeof(m))
	if r, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&m))); r == 0 {
		return MemFact{}, false
	}
	free, limit := gib(m.AvailPageFile), gib(m.TotalPageFile)
	return MemFact{RAMAvailableGiB: gib(m.AvailPhys), RAMTotalGiB: gib(m.TotalPhys), CommitFreeGiB: &free, CommitLimitGiB: &limit}, true
}

// listProcesses: alle Prozesse mit vollem Pfad. Der Agent laeuft als LocalSystem und liest damit fast alle Pfade;
// geschuetzte Prozesse (Anti-Cheat) verweigern auch ihm OpenProcess - das laesst sich nicht vorab pruefen, ihr Pfad bleibt
// leer und matchGame wertet sie ueber den Namen.
func listProcesses() []procInfo {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil
	}
	defer windows.CloseHandle(snap)
	var e windows.ProcessEntry32
	e.Size = uint32(unsafe.Sizeof(e))
	var out []procInfo
	for err = windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		if e.ProcessID == 0 || e.ProcessID == 4 { // Leerlauf- und System-Prozess: kein Programm
			continue
		}
		out = append(out, procInfo{Name: windows.UTF16ToString(e.ExeFile[:]), Path: processPath(e.ProcessID)})
	}
	return out
}

func processPath(pid uint32) string {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return ""
	}
	defer windows.CloseHandle(h)
	buf := make([]uint16, windows.MAX_LONG_PATH)
	n := uint32(len(buf))
	if windows.QueryFullProcessImageName(h, 0, &buf[0], &n) != nil {
		return ""
	}
	return windows.UTF16ToString(buf[:n])
}

// gameFolders: Steam-Bibliotheken plus Installationsorte bekannter Verlage aus den Deinstallationseintraegen
// (HKLM 64/32 Bit und jeder geladene Benutzer-Zweig - der Agent ist LocalSystem, HKCU waere seiner).
func gameFolders() []string {
	var folders []string
	steam := `C:\Program Files (x86)\Steam`
	if k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\WOW6432Node\Valve\Steam`, registry.QUERY_VALUE); err == nil {
		if p, _, err := k.GetStringValue("InstallPath"); err == nil && p != "" {
			steam = p
		}
		k.Close()
	}
	if b, err := os.ReadFile(steam + `\steamapps\libraryfolders.vdf`); err == nil {
		folders = append(folders, parseLibraryVDF(string(b))...)
	}
	type root struct {
		key  registry.Key
		path string
	}
	roots := []root{{registry.LOCAL_MACHINE, uninstallKey}, {registry.LOCAL_MACHINE, `SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall`}}
	if sids, err := registry.USERS.ReadSubKeyNames(-1); err == nil {
		for _, sid := range sids {
			if !strings.HasSuffix(sid, "_Classes") {
				roots = append(roots, root{registry.USERS, sid + `\` + uninstallKey})
			}
		}
	}
	for _, r := range roots {
		k, err := registry.OpenKey(r.key, r.path, registry.ENUMERATE_SUB_KEYS)
		if err != nil {
			continue // Zweig ohne Uninstall-Schluessel (Dienstkonten, Standardprofil)
		}
		names, _ := k.ReadSubKeyNames(-1)
		k.Close()
		for _, n := range names {
			e, err := registry.OpenKey(r.key, r.path+`\`+n, registry.QUERY_VALUE)
			if err != nil {
				continue
			}
			pub, _, _ := e.GetStringValue("Publisher")
			name, _, _ := e.GetStringValue("DisplayName")
			loc, _, _ := e.GetStringValue("InstallLocation")
			e.Close()
			if f, ok := publisherFolder(pub, name, loc); ok {
				folders = append(folders, f)
			}
		}
	}
	return uniqueFolders(folders, func(p string) bool {
		st, err := os.Stat(p)
		return err == nil && st.IsDir()
	})
}
