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

// procTree: auf Linux ist die Prozessgruppe des Kindes (Setpgid) der Baum. Sie bleibt bestehen, solange ein
// Mitglied lebt - auch wenn der Hauptprozess schon beendet ist. Gegenstueck zum Job-Objekt unter Windows.
type procTree struct{ pgid int }

func (g *childGroup) track(pid int) (*procTree, error) { return &procTree{pgid: pid}, nil }

// alive: auf Linux ohne Zaehlung (-1 = unbekannt); kill(-pgid, 0) sagt nur "mindestens einer lebt".
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
