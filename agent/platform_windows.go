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

// All Windows-specific logic lives here (Service API, Job Object, DPAPI, window suppression, Registry).
// platform_other.go provides the counterparts for Linux/macOS so the agent builds and runs in the
// foreground (run) there; service installation is not yet available (systemd unit later).

func hideWindow(cmd *exec.Cmd) { cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true} }

func childProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{HideWindow: true, CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
}

// childGroup: Job Object with KILL_ON_JOB_CLOSE - if the service dies, the children die with it.
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

// procTree is a separate Job Object per child, nested under the service Job. Anything the child
// starts lands in it automatically - including processes created later.
//
// Why: killChild only terminated the main process on Windows. Ollama starts its model as a separate
// process (llama-server). On 2026-09-23 the Supervisor restarted Ollama after failed
// health checks - the model process survived as an orphan with 23.5 GiB of GPU memory, the new
// Ollama did not know about it, the router counted it as foreign VRAM and kept the node "busy" for hours.
// The service Job Object only cleans up when the whole service dies; this one does so on every restart.
type procTree struct {
	mu  sync.Mutex
	job windows.Handle
}

// track attaches the child to the service Job Object and to its own, nested Job Object below it.
// Order: first the service Job, then the empty child Job - this makes it a sub-Job (Windows 8+).
// If only the child Job fails, the old protection (service Job) remains; the error is reported.
func (g *childGroup) track(pid int) (*procTree, error) {
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return nil, err
	}
	defer windows.CloseHandle(h)
	if err := windows.AssignProcessToJobObject(g.job, h); err != nil {
		return nil, fmt.Errorf("service job: %w", err)
	}
	job, err := newKillOnCloseJob()
	if err != nil {
		return nil, err
	}
	if err := windows.AssignProcessToJobObject(job, h); err != nil {
		windows.CloseHandle(job)
		return nil, fmt.Errorf("child job: %w", err)
	}
	return &procTree{job: job}, nil
}

// jobAccounting corresponds to JOBOBJECT_BASIC_ACCOUNTING_INFORMATION (not defined in x/sys/windows v0.35).
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

// alive: how many processes in the child's Job are still alive (-1 if undeterminable).
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

// kill terminates all processes in the child's Job - including grandchildren whose parents are already dead.
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

// release frees the handle. KILL_ON_JOB_CLOSE: anything still alive dies with it.
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

// protectBytes/unprotectBytes: DPAPI in machine context. The Private Key is thus never stored in plaintext on disk;
// the file ACL (service account, administrators) remains the second barrier.
func protectBytes(b []byte) ([]byte, error) { return dpapi(b, true) }

func unprotectBytes(b []byte) ([]byte, error) { return dpapi(b, false) }

func dpapi(b []byte, protect bool) ([]byte, error) {
	if len(b) == 0 {
		return nil, fmt.Errorf("empty data")
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
