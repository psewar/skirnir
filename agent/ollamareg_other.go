//go:build !windows

package main

// syncUninstallEntries: auf Linux gibt es keinen Deinstallationseintrag, den winget liest.
func syncUninstallEntries(dir, version string) ([]string, error) { return nil, nil }
