//go:build linux

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ADR-0070: `skirnir-agent setup` on Linux, staged below a folder with --root (no root rights, no systemctl).

const synthKey = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=" // 32 bytes, base64

func TestLinuxSetupStaged(t *testing.T) {
	root := t.TempDir()
	cfgPath := "/etc/skirnir-agent/config.yaml"
	var out bytes.Buffer
	args := []string{"setup", "--root", root, "--router", "https://router.example.invalid:11435", "--update-key", synthKey}
	if err := setupLinux(args, strings.NewReader(""), &out, cfgPath); err != nil {
		t.Fatalf("setup: %v\n%s", err, out.String())
	}
	if !strings.HasSuffix(strings.TrimSpace(out.String()), "STATE: installed") {
		t.Fatalf("last line must be STATE: installed:\n%s", out.String())
	}
	cfgFile := filepath.Join(root, cfgPath)
	st, err := os.Stat(cfgFile)
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("configuration must exist owner-only: %v %v", err, st)
	}
	text, _ := os.ReadFile(cfgFile)
	for _, want := range []string{"url: https://router.example.invalid:11435", "public_key: " + synthKey,
		"dir: /var/lib/skirnir-agent/identity", "dir: /var/log/skirnir-agent", "upstream: http://127.0.0.1:11434"} {
		if !strings.Contains(string(text), want) {
			t.Errorf("configuration lacks %q", want)
		}
	}
	for _, d := range []string{linuxStateDir, linuxLogDir} {
		if st, err := os.Stat(root + d); err != nil || !st.IsDir() || st.Mode().Perm() != 0o700 {
			t.Errorf("%s must exist with 0700: %v %v", d, err, st)
		}
	}
	unit, err := os.ReadFile(root + linuxUnitPath)
	if err != nil || !strings.Contains(string(unit), "ReadWritePaths=/var/lib/skirnir-agent /var/log/skirnir-agent") {
		t.Fatalf("unit must be written from the embedded file: %v", err)
	}
	if st, err := os.Stat(root + linuxBinary); err != nil || st.Mode().Perm()&0o111 == 0 {
		t.Fatalf("binary must be installed executable: %v", err)
	}
	// A second run keeps the configuration untouched.
	out.Reset()
	if err := setupLinux([]string{"setup", "--root", root, "--router", "https://other.example.invalid:11435"}, strings.NewReader(""), &out, cfgPath); err != nil {
		t.Fatalf("second setup: %v", err)
	}
	again, _ := os.ReadFile(cfgFile)
	if !bytes.Equal(text, again) || !strings.Contains(out.String(), "left unchanged") {
		t.Fatal("an existing configuration must stay unchanged")
	}
}

func TestLinuxSetupRefusals(t *testing.T) {
	cfgPath := "/etc/skirnir-agent/config.yaml"
	missingCA := filepath.Join(t.TempDir(), "missing.pem")
	cases := map[string][]string{
		"plain http router": {"--router", "http://router.example.invalid:11435"},
		"malformed key":     {"--router", "https://router.example.invalid:11435", "--update-key", "not-a-key"},
		"missing CA file":   {"--router", "https://router.example.invalid:11435", "--ca-file", missingCA},
	}
	for name, extra := range cases {
		root := t.TempDir()
		var out bytes.Buffer
		if err := setupLinux(append([]string{"setup", "--root", root}, extra...), strings.NewReader(""), &out, cfgPath); err == nil {
			t.Errorf("%s: setup must refuse", name)
		}
		if _, err := os.Stat(filepath.Join(root, cfgPath)); err == nil {
			t.Errorf("%s: a refused setup must not write a configuration", name)
		}
	}
	if os.Geteuid() != 0 {
		var out bytes.Buffer
		if err := setupLinux([]string{"setup", "--router", "https://router.example.invalid:11435"}, strings.NewReader(""), &out, cfgPath); err == nil || !strings.Contains(err.Error(), "root") {
			t.Fatalf("without --root setup must require root, got %v", err)
		}
	}
}
