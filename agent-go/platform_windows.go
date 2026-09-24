//go:build windows

package main

import (
	"fmt"
	"os/exec"
	"sync"
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

func newKillOnCloseJob() (windows.Handle, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return 0, fmt.Errorf("CreateJobObject: %w", err)
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		windows.CloseHandle(job)
		return 0, fmt.Errorf("SetInformationJobObject: %w", err)
	}
	return job, nil
}

func newChildGroup() (*childGroup, error) {
	job, err := newKillOnCloseJob()
	if err != nil {
		return nil, err
	}
	return &childGroup{job: job}, nil
}

// procTree ist ein eigenes Job-Objekt je Kind, verschachtelt unter dem Dienst-Job. Alles, was das Kind
// startet, landet automatisch mit darin - auch Prozesse, die erst spaeter entstehen.
//
// Warum: killChild beendete unter Windows nur den Hauptprozess. Ollama startet sein Modell aber als eigenen
// Prozess (llama-server). Am 2026-09-23 startete der Supervisor Ollama nach gescheiterten
// Gesundheitspruefungen neu - der Modellprozess ueberlebte als Waise mit 23,5 GiB Grafikspeicher, das neue
// Ollama kannte ihn nicht, der Router zaehlte ihn als fremdes VRAM und hielt den Knoten stundenlang "busy".
// Das Dienst-Job-Objekt raeumt nur auf, wenn der ganze Dienst stirbt; dieses hier bei jedem Neustart.
type procTree struct {
	mu  sync.Mutex
	job windows.Handle
}

// track haengt das Kind an das Dienst-Job-Objekt und an ein eigenes, darunter verschachteltes Job-Objekt.
// Reihenfolge: erst der Dienst-Job, dann der leere Kind-Job - so wird er zum Unter-Job (Windows 8+).
// Scheitert nur der Kind-Job, bleibt der alte Schutz (Dienst-Job) bestehen; der Fehler wird gemeldet.
func (g *childGroup) track(pid int) (*procTree, error) {
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return nil, err
	}
	defer windows.CloseHandle(h)
	if err := windows.AssignProcessToJobObject(g.job, h); err != nil {
		return nil, fmt.Errorf("Dienst-Job: %w", err)
	}
	job, err := newKillOnCloseJob()
	if err != nil {
		return nil, err
	}
	if err := windows.AssignProcessToJobObject(job, h); err != nil {
		windows.CloseHandle(job)
		return nil, fmt.Errorf("Kind-Job: %w", err)
	}
	return &procTree{job: job}, nil
}

// jobAccounting entspricht JOBOBJECT_BASIC_ACCOUNTING_INFORMATION (in x/sys/windows v0.35 nicht definiert).
type jobAccounting struct {
	TotalUserTime             int64
	TotalKernelTime           int64
	ThisPeriodTotalUserTime   int64
	ThisPeriodTotalKernelTime int64
	TotalPageFaultCount       uint32
	TotalProcesses            uint32
	ActiveProcesses           uint32
	TotalTerminatedProcesses  uint32
}

// alive: wie viele Prozesse im Job des Kindes noch leben (-1, wenn nicht ermittelbar).
func (t *procTree) alive() int {
	if t == nil {
		return -1
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.job == 0 {
		return -1
	}
	var info jobAccounting
	if err := windows.QueryInformationJobObject(t.job, windows.JobObjectBasicAccountingInformation,
		uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info)), nil); err != nil {
		return -1
	}
	return int(info.ActiveProcesses)
}

// kill beendet alle Prozesse im Job des Kindes - auch Enkel, deren Eltern schon tot sind.
func (t *procTree) kill() {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.job != 0 {
		_ = windows.TerminateJobObject(t.job, 1)
	}
}

// release gibt das Handle frei. KILL_ON_JOB_CLOSE: was dann noch lebt, stirbt dabei ebenfalls.
func (t *procTree) release() {
	if t == nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.job != 0 {
		windows.CloseHandle(t.job)
		t.job = 0
	}
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
