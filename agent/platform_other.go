//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"strings"
	"syscall"
)

// Counterparts to platform_windows.go for Linux/macOS. The agent runs in the foreground (run) or via
// a systemd unit; service installation via custom verbs is not yet available. Nothing here prevents this:
// Keys, tunnel, heartbeat, MQTT, and supervisor are platform-neutral.

func hideWindow(cmd *exec.Cmd) {}

func childProcAttr() *syscall.SysProcAttr { return &syscall.SysProcAttr{Setpgid: true} }

type childGroup struct{}

func newChildGroup() (*childGroup, error) { return &childGroup{}, nil }

// procTree: on Linux, the child's process group (Setpgid) is the tree. It persists as long as any
// member is alive - even if the main process has already exited. Counterpart to the Job object on Windows.
type procTree struct{ pgid int }

func (g *childGroup) track(pid int) (*procTree, error) { return &procTree{pgid: pid}, nil }

// alive: on Linux without counting (-1 = unknown); kill(-pgid, 0) only indicates "at least one is alive".
func (t *procTree) alive() int {
	if t == nil || t.pgid <= 0 {
		return -1
	}
	if err := syscall.Kill(-t.pgid, 0); err != nil {
		return 0
	}
	return -1
}

func (t *procTree) kill() {
	if t != nil && t.pgid > 0 {
		_ = syscall.Kill(-t.pgid, syscall.SIGKILL)
	}
}

func (t *procTree) release() {}

// killChild terminates the entire process group of the child (Setpgid above).
func killChild(cmd *exec.Cmd) {
	if cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		_ = cmd.Process.Kill()
	}
}

func runningAsService() bool { return false }

func runService(cfgPath, name string) error {
	return errors.New("service mode exists only on Windows; on Linux start 'run' via a systemd unit")
}

// No DPAPI: the key is stored as a file with mode 0600. Later: Kernel Keyring or TPM.
func protectBytes(b []byte) ([]byte, error) { return b, nil }

func unprotectBytes(b []byte) ([]byte, error) { return b, nil }

func systemModel() (string, string) {
	rd := func(p string) string {
		b, err := os.ReadFile(p)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(b))
	}
	return rd("/sys/class/dmi/id/sys_vendor"), rd("/sys/class/dmi/id/product_name")
}

var errNotWindows = errors.New("only available on Windows")

func install(cfg *Config, cfgPath string) error    { return errNotWindows }
func uninstall(cfg *Config) error                  { return errNotWindows }
func applyRules(cfg *Config, cfgPath string) error { return errNotWindows }
func controlService(name string, cmd string) error { return errNotWindows }
func serviceState(name string) (string, error)     { return "no service (Linux)", nil }
