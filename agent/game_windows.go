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

// memoryFact: available RAM (incl. Standby) and free commit - same numbers as FreePhysicalMemory /
// FreeVirtualMemory of the game watcher. TotalPageFile is the commit limit (RAM + page file).
func memoryFact() (MemFact, bool) {
	var m memoryStatusEx
	m.Length = uint32(unsafe.Sizeof(m))
	if r, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&m))); r == 0 {
		return MemFact{}, false
	}
	free, limit := gib(m.AvailPageFile), gib(m.TotalPageFile)
	return MemFact{RAMAvailableGiB: gib(m.AvailPhys), RAMTotalGiB: gib(m.TotalPhys), CommitFreeGiB: &free, CommitLimitGiB: &limit}, true
}

// listProcesses: all processes with full path. The agent runs as LocalSystem and thus reads almost all paths;
// protected processes (anti-cheat) deny OpenProcess to it as well - this cannot be checked in advance, their path remains
// empty and matchGame evaluates them by name.
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
		if e.ProcessID == 0 || e.ProcessID == 4 { // Idle and system process: no program
			continue
		}
		out = append(out, procInfo{Name: windows.UTF16ToString(e.ExeFile[:]), Path: processPath(e.ProcessID), PID: e.ProcessID})
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

// gameFolders: Steam libraries plus install locations of known publishers from uninstall entries
// (HKLM 64/32-bit and every loaded user hive - the agent is LocalSystem, HKCU would be its own).
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
			continue // Hive without Uninstall key (service accounts, default profile)
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
