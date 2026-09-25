//go:build !windows

package main

import "os"

// restartSelf: unter systemd (Restart=always) reicht Beenden; im Vordergrund ebenso.
func restartSelf(exe, cfgPath string, log *Logger) {
	log.Infof("update: beende mich fuer den Neustart (systemd Restart=always)")
	os.Exit(0)
}

func afterSwap(exe string, log *Logger) {}
