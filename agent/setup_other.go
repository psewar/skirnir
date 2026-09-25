//go:build !windows

package main

import "fmt"

func setupInteractive(cfgPath string) error {
	return fmt.Errorf("setup gibt es nur unter Windows; Linux: Binary nach /usr/local/bin, Config nach /etc/skirnir-agent, systemd-Unit (siehe config.example.yaml)")
}
