//go:build !windows

package main

// syncUninstallEntries: On Linux, there is no uninstall entry for winget to read.
func syncUninstallEntries(dir, version string) ([]string, error) { return nil, nil }
