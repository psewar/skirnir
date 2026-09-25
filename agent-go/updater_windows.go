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
	// weiter, hielt die .old-Datei und liess das naechste Update am Umbenennen scheitern). Darum alle anderen Prozesse
	// dieser Binary beenden - das sind nur Relay-Instanzen und der schon fertige Neustart-Helfer.
	kill := exec.Command("taskkill.exe", "/F", "/FI", "IMAGENAME eq "+filepath.Base(exe), "/FI", fmt.Sprintf("PID ne %d", os.Getpid()))
	hideWindow(kill)
	if out, err := kill.CombinedOutput(); err == nil {
		log.Infof("update: alte Prozesse der Binary beendet: %s", strings.TrimSpace(strings.Split(string(out), "\n")[0]))
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
