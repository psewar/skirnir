//go:build !windows && !linux

package main

import "fmt"

func setupInteractive(cfgPath string) error {
	return fmt.Errorf("setup exists on Windows and Linux; elsewhere install by hand (see agent/config.example.yaml)")
}
