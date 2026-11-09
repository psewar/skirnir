//go:build linux

package main

import (
	"bufio"
	_ "embed"
	"encoding/base64"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

//go:embed skirnir-agent.service
var linuxUnit string

const (
	linuxBinary   = "/usr/local/bin/skirnir-agent"
	linuxUnitPath = "/etc/systemd/system/skirnir-agent.service"
	linuxStateDir = "/var/lib/skirnir-agent"
	linuxLogDir   = "/var/log/skirnir-agent"
)

// setupInteractive (Linux, ADR-0070): `sudo skirnir-agent setup [--router URL] [--update-key KEY] [--ca-file PEM]` installs
// the agent as a systemd service. Asks for what is missing on stdin.
func setupInteractive(cfgPath string) error {
	return setupLinux(os.Args[1:], os.Stdin, os.Stdout, cfgPath)
}

// setupLinux checks before acting (root, router URL https://, update key format, CA file), then writes the configuration
// (only if none exists; owner-only) with identity.dir and logging.dir outside the read-only /etc, creates those folders,
// copies the binary to /usr/local/bin, writes the unit, validates the configuration like `check-config`, and - unless
// --root stages the files below a folder (packaging, tests) - reloads systemd and starts the service. Last line:
// STATE: installed.
func setupLinux(args []string, stdin io.Reader, out io.Writer, cfgPath string) error {
	opt := map[string]string{}
	for i := 0; i < len(args); i++ {
		for _, name := range []string{"--router", "--update-key", "--ca-file", "--root"} {
			if args[i] == name && i+1 < len(args) {
				opt[name] = args[i+1]
			} else if strings.HasPrefix(args[i], name+"=") {
				opt[name] = strings.TrimPrefix(args[i], name+"=")
			}
		}
	}
	root := opt["--root"]
	if root == "" && os.Geteuid() != 0 {
		return fmt.Errorf("run as root: sudo %s setup", os.Args[0])
	}
	in := bufio.NewReader(stdin)
	cfgFile := root + cfgPath
	if _, err := os.Stat(cfgFile); err == nil {
		fmt.Fprintln(out, "configuration exists, left unchanged:", cfgFile)
	} else {
		url := strings.TrimRight(opt["--router"], "/")
		if url == "" {
			url = strings.TrimRight(askLine(in, out, "Router address (e.g. https://router.example.net:11435): "), "/")
		}
		if !strings.HasPrefix(url, "https://") {
			return fmt.Errorf("the router address must start with https:// (got %q)", url)
		}
		key := opt["--update-key"]
		if _, set := opt["--update-key"]; !set && opt["--router"] == "" {
			key = askLine(in, out, "Operator public update key (base64; empty = add self-update later): ")
		}
		if key != "" {
			raw, err := base64.StdEncoding.DecodeString(key)
			if err != nil || len(raw) != 32 {
				return fmt.Errorf("the update key is not an Ed25519 public key (base64, 32 bytes)")
			}
		}
		caFile := opt["--ca-file"]
		if _, err := routerTLSConfig(caFile); err != nil {
			return err
		}
		var b strings.Builder
		fmt.Fprintf(&b, "# Skirnir agent - written by `skirnir-agent setup` on %s\n", time.Now().Format("2006-01-02 15:04"))
		b.WriteString("# All keys: agent/config.example.yaml in the repository (github.com/psewar/skirnir)\n\n")
		fmt.Fprintf(&b, "router:\n  url: %s\n", url)
		if caFile != "" {
			fmt.Fprintf(&b, "  ca_file: %s   # private CA of the router's certificate (ADR-0070)\n", caFile)
		}
		b.WriteString("\ntunnel:\n  upstream: http://127.0.0.1:11434   # Ollama on this node; keep it bound to loopback (OLLAMA_HOST=127.0.0.1)\n\n")
		if key != "" {
			fmt.Fprintf(&b, "update:\n  public_key: %s\n\n", key)
		} else {
			b.WriteString("# update:\n#   public_key: <base64>   # self-update through the router: the operator's public key\n\n")
		}
		fmt.Fprintf(&b, "identity:\n  dir: %s/identity   # node key (0600); /etc is read-only for the service\n\n", linuxStateDir)
		fmt.Fprintf(&b, "logging:\n  dir: %s\n", linuxLogDir)
		if err := os.MkdirAll(filepath.Dir(cfgFile), 0o755); err != nil {
			return err
		}
		f, err := os.OpenFile(cfgFile, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if err != nil {
			return err
		}
		if _, err := f.WriteString(b.String()); err != nil {
			f.Close()
			return err
		}
		if err := f.Close(); err != nil {
			return err
		}
		fmt.Fprintln(out, "configuration written:", cfgFile)
	}
	for _, d := range []string{linuxStateDir, linuxLogDir} {
		if err := os.MkdirAll(root+d, 0o700); err != nil {
			return err
		}
	}
	if err := installLinuxBinary(root + linuxBinary); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(root+linuxUnitPath), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(root+linuxUnitPath, []byte(linuxUnit), 0o644); err != nil {
		return err
	}
	if _, err := loadConfig(cfgFile); err != nil { // validates as `check-config` does
		return fmt.Errorf("the configuration %s does not load: %w", cfgFile, err)
	}
	fmt.Fprintln(out, "binary:", root+linuxBinary, " unit:", root+linuxUnitPath)
	if root == "" {
		for _, c := range [][]string{{"daemon-reload"}, {"enable", "--now", "skirnir-agent"}} {
			if o, err := exec.Command("systemctl", c...).CombinedOutput(); err != nil {
				return fmt.Errorf("systemctl %s: %v %s", strings.Join(c, " "), err, strings.TrimSpace(string(o)))
			}
		}
		fmt.Fprintln(out, "service skirnir-agent started.")
	}
	fmt.Fprintf(out, "Next: in the router's web UI the node appears as pending. Compare its fingerprint with\n"+
		"  %s identity --config %s\nand approve it only if they match.\n", linuxBinary, cfgPath)
	fmt.Fprintln(out, "STATE: installed")
	return nil
}

func askLine(in *bufio.Reader, out io.Writer, prompt string) string {
	fmt.Fprint(out, prompt)
	line, _ := in.ReadString('\n') // end of input = empty answer
	return strings.TrimSpace(line)
}

// installLinuxBinary copies the running binary to dst (temporary file and rename), unless it already runs from there.
func installLinuxBinary(dst string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	if filepath.Clean(exe) == filepath.Clean(dst) {
		return nil
	}
	src, err := os.ReadFile(exe)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	tmp := dst + ".setup"
	if err := os.WriteFile(tmp, src, 0o755); err != nil {
		return err
	}
	return os.Rename(tmp, dst)
}
