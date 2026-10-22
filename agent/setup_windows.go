//go:build windows

package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Einrichtung per Doppelklick (0.10.0): `skirnir-agent.exe` ohne Argumente (oder mit `setup`) hebt sich per UAC an, fragt
// beim ersten Mal nach Router-URL, Update-Schluessel und dem Benutzer, der den Dienst steuern darf, legt die Konfiguration
// als LocalSystem-Dienst an, kopiert sich nach Program Files, installiert oder aktualisiert den Dienst "Skirnir Agent",
// legt die GPU-Z-Relay-Aufgabe an und zeigt am Ende Zustand und Fingerprint. Ersetzt Install-Service.ps1 fuer Menschen;
// das Skript bleibt fuer Sonderfaelle (alte Tasks entfernen, Firewall-Regeln des Ollama-Installers).
//
// Anlass 2026-09-25: Install-Service.ps1 pruefte die Admin-Rolle ueber den ENGLISCHEN Gruppennamen und verweigerte auf einem
// deutschen Windows trotz erhoehter PowerShell. Hier entscheidet das Token (IsElevated), nicht ein Gruppenname.

const (
	setupDstDir = `C:\Program Files\skirnir-agent`
	setupExe    = "skirnir-agent.exe"
)

func isElevated() bool {
	var t windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_QUERY, &t); err != nil {
		return false
	}
	defer t.Close()
	return t.IsElevated()
}

// relaunchElevated startet dieselbe Binary mit UAC-Abfrage neu (eigenes Konsolenfenster).
func relaunchElevated(args []string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	quoted := make([]string, len(args))
	for i, a := range args {
		quoted[i] = `"` + a + `"`
	}
	verb, _ := syscall.UTF16PtrFromString("runas")
	file, _ := syscall.UTF16PtrFromString(exe)
	arg, _ := syscall.UTF16PtrFromString(strings.Join(quoted, " "))
	return windows.ShellExecute(0, verb, file, arg, nil, windows.SW_SHOWNORMAL)
}

// ownConsole: true, wenn dieses Fenster nur uns gehoert (Doppelklick oder UAC-Neustart) - dann am Ende auf Enter warten,
// sonst verschwindet die Ausgabe mit dem Fenster.
func ownConsole() bool {
	k := windows.NewLazySystemDLL("kernel32.dll").NewProc("GetConsoleProcessList")
	var pids [4]uint32
	n, _, _ := k.Call(uintptr(unsafe.Pointer(&pids[0])), uintptr(len(pids)))
	return n == 1
}

func pause() {
	if ownConsole() {
		fmt.Print("\nEnter to close ... ")
		_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
	}
}

func ask(r *bufio.Reader, prompt, def string) string {
	if def != "" {
		fmt.Printf("%s [%s]: ", prompt, def)
	} else {
		fmt.Printf("%s: ", prompt)
	}
	s, _ := r.ReadString('\n')
	s = strings.TrimSpace(s)
	if s == "" {
		return def
	}
	return s
}

// setupInteractive: die ganze Einrichtung; ruft install/apply-rules/start ueber die INSTALLIERTE Binary auf, damit der
// Dienst im SCM auf Program Files zeigt und nicht auf den Download-Ordner.
func setupInteractive(cfgPath string) error {
	fmt.Printf("Skirnir Agent %s - setup\n\n", version)
	if !isElevated() {
		fmt.Println("This needs administrator rights - Windows will ask (UAC).")
		if err := relaunchElevated([]string{"setup", "--config", cfgPath}); err != nil {
			pause()
			return fmt.Errorf("elevation refused or failed: %w", err)
		}
		return nil
	}
	err := setupBody(cfgPath)
	if err != nil { // vor der Enter-Pause ausgeben, damit es im UAC-Fenster nicht untergeht (2026-09-26)
		fmt.Printf("\n*** ERROR: %v ***\n", err)
	}
	pause()
	return err
}

// setupBody: die eigentliche Einrichtung (erhoeht).
func setupBody(cfgPath string) error {
	in := bufio.NewReader(os.Stdin)
	if moved, err := migrateLegacyInstall(&cfgPath); err != nil {
		return err
	} else if moved {
		fmt.Println("Old installation (ollama-router-agent) moved:", filepath.Dir(cfgPath))
	}
	if _, err := os.Stat(cfgPath); os.IsNotExist(err) {
		if err := setupWriteConfig(in, cfgPath); err != nil {
			return err
		}
	} else {
		fmt.Println("configuration present:", cfgPath)
		if err := setupUpgradeConfig(in, cfgPath); err != nil {
			return err
		}
	}
	cfg, err := loadConfig(cfgPath)
	if err != nil {
		return fmt.Errorf("configuration: %w", err)
	}
	dst := filepath.Join(setupDstDir, setupExe)
	exe, _ := os.Executable()
	exe, _ = filepath.Abs(exe)
	name := cfg.Install.ServiceName
	st, _ := serviceState(name)
	exists := st != serviceNotInstalled

	// Dienst und Relay anhalten, damit die Binary ersetzt werden kann
	if exists {
		fmt.Printf("service %s: %s -> stop\n", name, st)
		_ = controlService(name, "stop")
	}
	stopRelayProcesses()
	if !strings.EqualFold(exe, dst) {
		if err := os.MkdirAll(setupDstDir, 0o755); err != nil {
			return err
		}
		if err := copyFile(exe, dst); err != nil {
			return fmt.Errorf("copy binary to %s: %w", dst, err)
		}
		fmt.Println("binary copied to", dst)
	}
	// Konto geaendert (z. B. NT SERVICE -> LocalSystem)? Dann neu anlegen - das Konto laesst sich nur beim Anlegen setzen.
	if exists {
		if acct, err := serviceAccount(name); err == nil && !sameAccount(acct, cfg.Install.Account) {
			fmt.Printf("service account is %q, configuration wants %q -> service is recreated (identity and logs stay)\n", acct, firstNonEmpty(cfg.Install.Account, "LocalSystem"))
			if err := runVisible(dst, "uninstall", "--config", cfgPath); err != nil {
				return err
			}
			exists = false
		}
	}
	if exists {
		if err := runVisible(dst, "apply-rules", "--config", cfgPath); err != nil {
			return err
		}
	} else if err := runVisible(dst, "install", "--config", cfgPath); err != nil {
		return err
	}
	if err := runVisible(dst, "start", "--config", cfgPath); err != nil {
		return err
	}
	if cfg.GPUZ.on() {
		if err := installRelayTask(dst, cfgPath); err != nil {
			fmt.Println("  GPU-Z relay task:", err, "(without the relay, NVML sensors keep running)")
		} else {
			fmt.Println("  GPU-Z relay task created (at logon, without admin rights)")
		}
	}
	if old := `C:\Program Files\ollama-router-agent`; !strings.EqualFold(old, setupDstDir) {
		if _, err := os.Stat(old); err == nil {
			if err := os.RemoveAll(old); err == nil {
				fmt.Println("Old program folder removed:", old)
			}
		}
	}
	fmt.Println("\nWaiting for the service ...")
	time.Sleep(6 * time.Second)
	printHealth(cfg.Health.Listen)
	fmt.Println("\nNext step: router UI -> tab Agents -> approve this computer (fingerprint see above).")
	return nil
}

// migrateLegacyInstall (0.11.0): liegt die Konfiguration noch unter ollama-router-agent und die neue fehlt, wird das
// Verzeichnis (Config, Identitaet, Provisionierung, Logs) nach skirnir-agent verschoben, Pfade in der Config nachgezogen,
// der alte Dienst samt Aufgabe entfernt. Danach zeigt cfgPath auf die neue Datei; install legt den Dienst SkirnirAgent an.
func migrateLegacyInstall(cfgPath *string) (bool, error) {
	newDefault, _ := filepath.Abs(strings.TrimSuffix(`C:\ProgramData\skirnir-agent\config.yaml`, ""))
	cur, _ := filepath.Abs(*cfgPath)
	if !strings.EqualFold(cur, newDefault) && !strings.EqualFold(cur, legacyConfigPathWindows) {
		return false, nil // ausdruecklich anderer Pfad: nichts verschieben
	}
	if _, err := os.Stat(newDefault); err == nil {
		*cfgPath = newDefault
		return false, nil
	}
	if _, err := os.Stat(legacyConfigPathWindows); err != nil {
		*cfgPath = newDefault
		return false, nil
	}
	fmt.Println("Old installation found - moving to skirnir-agent (service, directory, task) ...")
	// Erst die umgeschriebene Konfiguration pruefen, dann anfassen: ein Umzug, der an der Konfiguration scheitert, liesse den
	// Knoten ohne Dienst zurueck (gesehen 2026-09-26 auf einem Knoten mit alter Config).
	raw, err := os.ReadFile(legacyConfigPathWindows)
	if err != nil {
		return false, err
	}
	s := upgradeConfigText(string(raw))
	probe := filepath.Join(os.TempDir(), "skirnir-agent-umzug-probe.yaml")
	if err := os.WriteFile(probe, []byte(s), 0o600); err != nil {
		return false, err
	}
	_, cfgErr := loadConfig(probe)
	os.Remove(probe)
	if cfgErr != nil {
		return false, fmt.Errorf("the existing configuration would be invalid after the move - nothing changed, the old service keeps running.\n  %v\n  Please fix %s by hand and run setup again", cfgErr, legacyConfigPathWindows)
	}
	_ = controlService(legacyServiceName, "stop")
	stopRelayProcesses()
	runQuiet("schtasks.exe", "/Delete", "/F", "/TN", legacyRelayTaskName)
	oldDir, newDir := filepath.Dir(legacyConfigPathWindows), filepath.Dir(newDefault)
	if err := os.Rename(oldDir, newDir); err != nil {
		return false, fmt.Errorf("move %s to %s: %w", oldDir, newDir, err)
	}
	_ = copyFile(newDefault, newDefault+".vor-umzug-"+time.Now().Format("20060102-150405"))
	if err := os.WriteFile(newDefault, []byte(s), 0o600); err != nil {
		return false, err
	}
	c := &Config{}
	c.Install.ServiceName = legacyServiceName
	c.applyDefaults()
	if err := uninstall(c); err != nil {
		fmt.Println("  old service:", err)
	}
	*cfgPath = newDefault
	return true, nil
}

// upgradeConfigText: Pfade und Namen der alten Installation, alter Blockname mimir: -> secret_store: (gleiche Schluessel).
func upgradeConfigText(s string) string {
	for old, repl := range map[string]string{`\ollama-router-agent\`: `\skirnir-agent\`, "OllamaRouterAgent": "SkirnirAgent", "Ollama Router Agent": "Skirnir Agent"} {
		s = strings.ReplaceAll(s, old, repl)
	}
	lines := strings.Split(s, "\n")
	for i, l := range lines {
		if strings.TrimRight(l, "\r") == "mimir:" {
			lines[i] = "secret_store:   # hiess bis 0.9.0 mimir: (setup hat umbenannt)"
		}
	}
	return strings.Join(lines, "\n")
}

func setupWriteConfig(in *bufio.Reader, cfgPath string) error {
	fmt.Println("No configuration yet - three questions:")
	url := ""
	for url == "" {
		url = strings.TrimRight(ask(in, "Router address (e.g. https://router.example.net:11435)", ""), "/")
		if !strings.HasPrefix(url, "https://") && !strings.HasPrefix(url, "http://") {
			fmt.Println("  please include https://")
			url = ""
		}
	}
	key := ask(in, "Operator public update key (base64; empty = add self-update later)", "")
	user := ask(in, "User allowed to control the service without admin rights", os.Getenv("COMPUTERNAME")+`\`+os.Getenv("USERNAME"))
	var b strings.Builder
	fmt.Fprintf(&b, "# Skirnir Agent - created by `skirnir-agent.exe setup` on %s\n", time.Now().Format("2006-01-02 15:04"))
	b.WriteString("# All keys: config.example.yaml in the repo (github.com/psewar/skirnir, agent/)\n\n")
	fmt.Fprintf(&b, "router:\n  url: %s\n\ntunnel:\n  upstream: http://127.0.0.1:11434\n\n", url)
	if key != "" {
		fmt.Fprintf(&b, "update:\n  public_key: %s\n\n", key)
	} else {
		b.WriteString("# update:\n#   public_key: <base64>   # self-update via the router: key from the operator (deploy.py --agent)\n\n")
	}
	b.WriteString("install:\n  service_name: SkirnirAgent\n  display_name: Skirnir Agent\n  account: ''   # LocalSystem: GPU guard (power limit) and GPU-Z directly\n")
	fmt.Fprintf(&b, "  allow_control_users:\n    - '%s'\n", user)
	if err := os.MkdirAll(filepath.Dir(cfgPath), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(cfgPath, []byte(b.String()), 0o600); err != nil {
		return err
	}
	fmt.Println("configuration written:", cfgPath)
	return nil
}

// setupUpgradeConfig: bestehende Datei behutsam nachziehen - Update-Schluessel ergaenzen, altes NT-SERVICE-Konto auf
// LocalSystem, alter Blockname mimir: melden.
func setupUpgradeConfig(in *bufio.Reader, cfgPath string) error {
	raw, err := os.ReadFile(cfgPath)
	if err != nil {
		return err
	}
	s := string(raw)
	changed := false
	if !strings.Contains(s, "public_key:") {
		if key := ask(in, "Operator update key missing (update.public_key) - enter it now (empty = later)", ""); key != "" {
			s = strings.TrimRight(s, "\r\n") + "\n\n# self-update via the router: the manifest must be signed with the operator key\nupdate:\n  public_key: " + key + "\n"
			changed = true
		}
	}
	if strings.Contains(s, "NT SERVICE") {
		if strings.EqualFold(ask(in, "Service account is NT SERVICE (no GPU guard). Switch to LocalSystem? (y/n)", "y"), "y") {
			lines := strings.Split(s, "\n")
			for i, l := range lines {
				if strings.Contains(l, "account:") && strings.Contains(l, "NT SERVICE") {
					lines[i] = "  account: ''   # LocalSystem (setup " + time.Now().Format("2006-01-02") + "); vorher NT SERVICE-Konto"
				}
			}
			s = strings.Join(lines, "\n")
			changed = true
		}
	}
	if strings.Contains(s, "\nmimir:") {
		s = upgradeConfigText(s)
		fmt.Println("  block `mimir:` renamed to `secret_store:` (same keys)")
		changed = true
	}
	if _, err := loadConfig(cfgPath); err == nil {
		// Probe der geaenderten Fassung, bevor sie geschrieben wird
		probe := filepath.Join(os.TempDir(), "skirnir-agent-probe.yaml")
		if os.WriteFile(probe, []byte(s), 0o600) == nil {
			if _, perr := loadConfig(probe); perr != nil {
				os.Remove(probe)
				return fmt.Errorf("configuration would be invalid - nothing written: %w", perr)
			}
			os.Remove(probe)
		}
	}
	if changed {
		_ = copyFile(cfgPath, cfgPath+".bak-"+time.Now().Format("20060102-150405"))
		if err := os.WriteFile(cfgPath, []byte(s), 0o600); err != nil {
			return err
		}
		fmt.Println("configuration updated (backup next to it)")
	}
	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

func runVisible(exe string, args ...string) error {
	fmt.Printf("> %s %s\n", filepath.Base(exe), strings.Join(args, " "))
	cmd := exec.Command(exe, args...)
	cmd.Stdout, cmd.Stderr, cmd.Stdin = os.Stdout, os.Stderr, os.Stdin
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("%s %s: %w", filepath.Base(exe), args[0], err)
	}
	return nil
}

func stopRelayProcesses() {
	ps := `Get-ScheduledTask -TaskName '` + gpuzRelayTaskName + `' -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue; ` +
		`Get-ScheduledTask -TaskName '` + legacyRelayTaskName + `' -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue; ` +
		`Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq '` + setupExe + `' -or $_.Name -eq 'ollama-router-agent.exe') -and $_.CommandLine -like '*gpuz-relay*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`
	runQuiet("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps)
	time.Sleep(time.Second)
}

// installRelayTask: wie Install-GpuzRelayTask in Install-Service.ps1 - Aufgabe bei Anmeldung, Gruppe Benutzer, versteckt,
// conhost --headless (ein Terminalfenster wuerde sonst mit der Sitzung sterben).
func installRelayTask(exe, cfgPath string) error {
	ps := `$tn = '` + gpuzRelayTaskName + `'; ` +
		`$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument ('--headless "' + '` + exe + `' + '" gpuz-relay --config "' + '` + cfgPath + `' + '"'); ` +
		`$trigger = New-ScheduledTaskTrigger -AtLogOn; ` +
		`$settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval ([TimeSpan]::FromMinutes(1)) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; ` +
		`$principal = New-ScheduledTaskPrincipal -GroupId 'S-1-5-32-545' -RunLevel Limited; ` +
		`Register-ScheduledTask -TaskName $tn -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null; ` +
		`Start-ScheduledTask -TaskName $tn`
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps)
	hideWindow(cmd)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("%v: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}
