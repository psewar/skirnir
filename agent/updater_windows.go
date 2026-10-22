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
	gpuzRelayTaskName       = "SkirnirAgent-GpuzRelay"
	legacyRelayTaskName     = "OllamaRouterAgent-GpuzRelay" // up to 0.10.0
	restartFallbackExitWait = 60 * time.Second
)

// restartSelf restarts the service using the NEW binary: `restart` (SCM stop + start) runs in a separate,
// process detached from the job, surviving the service stop. In console mode, termination is sufficient.
func restartSelf(exe, cfgPath string, log *Logger) {
	if !runningAsService() {
		log.Infof("update: console mode - exiting, please restart")
		os.Exit(0)
	}
	cmd := exec.Command(exe, "restart", "--config", cfgPath)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNewProcessGroup | createBreakawayFromJob | detachedProcess}
	if err := cmd.Start(); err != nil {
		log.Warnf("update: restart process: %v - exiting, service recovery restarts", err)
		os.Exit(4)
	}
	log.Infof("update: restart triggered (PID %d)", cmd.Process.Pid)
	// If the SCM stop doesn't arrive (missing control rights, etc.): self-terminate after a timeout; Recovery restarts it.
	go func() {
		time.Sleep(restartFallbackExitWait)
		log.Warnf("update: no stop from the service manager after %s - exiting myself", restartFallbackExitWait)
		os.Exit(4)
	}()
}

// afterSwap: the GPU-Z relay (task in the logon session) is still running from the old binary - restart it so it
// uses the new one and releases the .old file. No error if the task doesn't exist.
func afterSwap(exe string, log *Logger) {
	// `schtasks /End` does not reliably terminate the relay from the old binary (observed 2026-09-25: the old relay kept
	// running, held the .old file, and caused the next update to fail during rename). Therefore, specifically terminate the relay processes
	// of this binary - only those with `gpuz-relay` in the command line, not every invocation of the binary
	// (status, identity, a second service with different config).
	ps := fmt.Sprintf(`Get-CimInstance Win32_Process -Filter "Name = '%s'" | Where-Object { $_.CommandLine -like '*gpuz-relay*' -and $_.ProcessId -ne %d } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }`,
		filepath.Base(exe), os.Getpid())
	kill := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps)
	hideWindow(kill)
	if out, err := kill.CombinedOutput(); err == nil && strings.TrimSpace(string(out)) != "" {
		log.Infof("update: old relay processes ended (PID %s)", strings.Join(strings.Fields(string(out)), ", "))
	} else if err != nil {
		log.Debugf("update: ending relay processes: %v %s", err, strings.TrimSpace(string(out)))
	}
	for _, tn := range []string{gpuzRelayTaskName, legacyRelayTaskName} { // old task name until setup migrated it
		ok := true
		for _, args := range [][]string{{"/End", "/TN", tn}, {"/Run", "/TN", tn}} {
			cmd := exec.Command("schtasks.exe", args...)
			hideWindow(cmd)
			if out, err := cmd.CombinedOutput(); err != nil {
				log.Debugf("update: schtasks %v: %v %s", args, err, string(out))
				ok = false
				break
			}
		}
		if ok {
			log.Infof("update: GPU-Z relay task %s restarted", tn)
			break
		}
	}
	time.Sleep(2 * time.Second)
}
