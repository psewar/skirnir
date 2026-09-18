//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
	"golang.org/x/sys/windows/svc"
)

// Alles Windows-Spezifische liegt hier (Dienst-API, Job-Objekt, DPAPI, Fensterunterdrueckung, Registry).
// platform_other.go liefert die Gegenstuecke fuer Linux/macOS, damit der Agent dort ebenfalls baut und im
// Vordergrund (run) laeuft; Dienst-Installation gibt es dort noch nicht (systemd-Unit spaeter).

func hideWindow(cmd *exec.Cmd) { cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true} }

func childProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{HideWindow: true, CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}

// childGroup: Job-Objekt mit KILL_ON_JOB_CLOSE - stirbt der Dienst, sterben die Kinder mit.
type childGroup struct{ job windows.Handle }

func newChildGroup() (*childGroup, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return nil, fmt.Errorf("CreateJobObject: %w", err)
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		return nil, fmt.Errorf("SetInformationJobObject: %w", err)
	}
	return &childGroup{job: job}, nil
}

func (g *childGroup) add(pid int) error {
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	return windows.AssignProcessToJobObject(g.job, h)
}

func killChild(cmd *exec.Cmd) { _ = cmd.Process.Kill() }

func runningAsService() bool {
	ok, _ := svc.IsWindowsService()
	return ok
}

func runService(cfgPath, name string) error {
	return svc.Run(name, &windowsService{cfgPath: cfgPath, name: name})
}

// protectBytes/unprotectBytes: DPAPI im Maschinenkontext. Der Private Key liegt damit nie im Klartext auf der Platte;
// die Datei-ACL (Dienstkonto, Administratoren) bleibt die zweite Huerde.
func protectBytes(b []byte) ([]byte, error) { return dpapi(b, true) }

func unprotectBytes(b []byte) ([]byte, error) { return dpapi(b, false) }

func dpapi(b []byte, protect bool) ([]byte, error) {
	if len(b) == 0 {
		return nil, fmt.Errorf("leere Daten")
	}
	in := windows.DataBlob{Size: uint32(len(b)), Data: &b[0]}
	var out windows.DataBlob
	flags := uint32(windows.CRYPTPROTECT_UI_FORBIDDEN | windows.CRYPTPROTECT_LOCAL_MACHINE)
	var err error
	if protect {
		err = windows.CryptProtectData(&in, nil, nil, 0, nil, flags, &out)
	} else {
		err = windows.CryptUnprotectData(&in, nil, nil, 0, nil, flags, &out)
	}
	if err != nil {
		return nil, fmt.Errorf("DPAPI: %w", err)
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(out.Data)))
	res := make([]byte, out.Size)
	copy(res, unsafe.Slice(out.Data, out.Size))
	return res, nil
}

func systemModel() (string, string) {
	k, err := registry.OpenKey(registry.LOCAL_MACHINE, `HARDWARE\DESCRIPTION\System\BIOS`, registry.QUERY_VALUE)
	if err != nil {
		return "", ""
	}
	defer k.Close()
	man, _, _ := k.GetStringValue("SystemManufacturer")
	mod, _, _ := k.GetStringValue("SystemProductName")
	return man, mod
}
