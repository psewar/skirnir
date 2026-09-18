//go:build !windows

package main

import (
	"errors"
	"os"
	"os/exec"
	"strings"
	"syscall"
)

// Gegenstuecke zu platform_windows.go fuer Linux/macOS. Der Agent laeuft dort im Vordergrund (run) oder per
// systemd-Unit; Dienst-Installation ueber die eigenen Verben gibt es noch nicht. Nichts hier verbaut das:
// Schluessel, Tunnel, Heartbeat, MQTT, Supervisor sind plattformneutral.

func hideWindow(cmd *exec.Cmd) {}

func childProcAttr() *syscall.SysProcAttr { return &syscall.SysProcAttr{Setpgid: true} }

type childGroup struct{}

func newChildGroup() (*childGroup, error) { return &childGroup{}, nil }

func (g *childGroup) add(pid int) error { return nil }

// killChild beendet die ganze Prozessgruppe des Kindes (Setpgid oben).
func killChild(cmd *exec.Cmd) {
	if cmd.Process != nil {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		_ = cmd.Process.Kill()
	}
}

func runningAsService() bool { return false }

func runService(cfgPath, name string) error {
	return errors.New("Dienstmodus gibt es nur unter Windows; auf Linux 'run' per systemd-Unit starten")
}

// Kein DPAPI: der Schluessel liegt als Datei mit Modus 0600. Spaeter: Kernel-Keyring oder TPM.
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

var errNotWindows = errors.New("nur unter Windows verfuegbar")

func install(cfg *Config, cfgPath string) error    { return errNotWindows }
func uninstall(cfg *Config) error                  { return errNotWindows }
func applyRules(cfg *Config, cfgPath string) error { return errNotWindows }
func controlService(name string, cmd string) error { return errNotWindows }
func serviceState(name string) (string, error)     { return "kein Dienst (Linux)", nil }
