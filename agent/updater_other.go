//go:build !windows

package main

import "os"

// restartSelf: under systemd (Restart=always), stopping is sufficient; same in the foreground.
func restartSelf(exe, cfgPath string, log *Logger) {
	log.Infof("update: exiting for the restart (systemd Restart=always)")
	os.Exit(0)
}

func afterSwap(exe string, log *Logger) {}
