//go:build !windows

package main

import "fmt"

func setupInteractive(cfgPath string) error {
	return fmt.Errorf("setup exists only on Windows; Linux: binary to /usr/local/bin, config to /etc/skirnir-agent, systemd unit (see config.example.yaml)")
}
