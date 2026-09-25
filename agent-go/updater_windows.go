//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const (
	createNewProcessGroup   = 0x00000200
	createBreakawayFromJob  = 0x01000000
	detachedProcess         = 0x00000008
	gpuzRelayTaskName       = "OllamaRouterAgent-GpuzRelay"
	restartFallbackExitWait = 60 * time.Second
)

// restartSelf startet den Dienst ueber die NEUE Binary neu: `restart` (SCM stop + start) laeuft in einem eigenen,
// vom Job losgeloesten Prozess, der den Stopp des Dienstes ueberlebt. Im Konsolenbetrieb reicht Beenden.
func restartSelf(exe, cfgPath string, log *Logger) {
	if !runningAsService() {
		log.Infof("update: Konsolenbetrieb - beende mich, bitte neu starten")
		os.Exit(0)
	}
	cmd := exec.Command(exe, "restart", "--config", cfgPath)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNewProcessGroup | createBreakawayFromJob | detachedProcess}
	if err := cmd.Start(); err != nil {
		log.Warnf("update: Neustart-Prozess: %v - beende mich, Recovery des Dienstes startet neu", err)
		os.Exit(4)
	}
	log.Infof("update: Neustart angestossen (PID %d)", cmd.Process.Pid)
	// Falls der SCM-Stopp nicht kommt (Steuerrecht fehlt o. ae.): nach einer Frist selbst beenden, Recovery startet neu.
	go func() {
		time.Sleep(restartFallbackExitWait)
		log.Warnf("update: kein Stopp vom Dienstmanager nach %s - beende mich selbst", restartFallbackExitWait)
		os.Exit(4)
	}()
}

// afterSwap: das GPU-Z-Relay (Aufgabe in der Anmeldesitzung) laeuft noch aus der alten Binary - neu starten, damit es
// die neue nutzt und die .old-Datei freigibt. Kein Fehler, wenn es die Aufgabe nicht gibt.
func afterSwap(exe string, log *Logger) {
	// `schtasks /End` beendet nicht zuverlaessig das Relay aus der alten Binary (gesehen 2026-09-25: das alte Relay lief
	// weiter, hielt die .old-Datei und liess das naechste Update am Umbenennen scheitern). Darum die Relay-Prozesse
	// dieser Binary gezielt beenden - nur die mit `gpuz-relay` in der Kommandozeile, nicht jeden Aufruf der Binary
	// (status, identity, ein zweiter Dienst mit anderer Config).
	ps := fmt.Sprintf(`Get-CimInstance Win32_Process -Filter "Name = '%s'" | Where-Object { $_.CommandLine -like '*gpuz-relay*' -and $_.ProcessId -ne %d } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }`,
		filepath.Base(exe), os.Getpid())
	kill := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps)
	hideWindow(kill)
	if out, err := kill.CombinedOutput(); err == nil && strings.TrimSpace(string(out)) != "" {
		log.Infof("update: alte Relay-Prozesse beendet (PID %s)", strings.Join(strings.Fields(string(out)), ", "))
	} else if err != nil {
		log.Debugf("update: Relay-Prozesse beenden: %v %s", err, strings.TrimSpace(string(out)))
	}
	for _, args := range [][]string{{"/End", "/TN", gpuzRelayTaskName}, {"/Run", "/TN", gpuzRelayTaskName}} {
		cmd := exec.Command("schtasks.exe", args...)
		hideWindow(cmd)
		if out, err := cmd.CombinedOutput(); err != nil {
			log.Debugf("update: schtasks %v: %v %s", args, err, string(out))
			return
		}
	}
	log.Infof("update: GPU-Z-Relay-Aufgabe neu gestartet")
	time.Sleep(2 * time.Second)
}
