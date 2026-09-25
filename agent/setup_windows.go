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
		fmt.Print("\nEnter zum Schliessen ... ")
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
	fmt.Printf("Skirnir Agent %s - Einrichtung\n\n", version)
	if !isElevated() {
		fmt.Println("Dafuer sind Administratorrechte noetig - Windows fragt gleich nach (UAC).")
		if err := relaunchElevated([]string{"setup", "--config", cfgPath}); err != nil {
			pause()
			return fmt.Errorf("Anhebung abgelehnt oder fehlgeschlagen: %w", err)
		}
		return nil
	}
	defer pause()
	in := bufio.NewReader(os.Stdin)
	if moved, err := migrateLegacyInstall(&cfgPath); err != nil {
		return err
	} else if moved {
		fmt.Println("Alte Installation (ollama-router-agent) umgezogen:", filepath.Dir(cfgPath))
	}
	if _, err := os.Stat(cfgPath); os.IsNotExist(err) {
		if err := setupWriteConfig(in, cfgPath); err != nil {
			return err
		}
	} else {
		fmt.Println("Konfiguration vorhanden:", cfgPath)
		if err := setupUpgradeConfig(in, cfgPath); err != nil {
			return err
		}
	}
	cfg, err := loadConfig(cfgPath)
	if err != nil {
		return fmt.Errorf("Konfiguration: %w", err)
	}
	dst := filepath.Join(setupDstDir, setupExe)
	exe, _ := os.Executable()
	exe, _ = filepath.Abs(exe)
	name := cfg.Install.ServiceName
	st, _ := serviceState(name)
	exists := st != "nicht installiert"

	// Dienst und Relay anhalten, damit die Binary ersetzt werden kann
	if exists {
		fmt.Printf("Dienst %s: %s -> stoppen\n", name, st)
		_ = controlService(name, "stop")
	}
	stopRelayProcesses()
	if !strings.EqualFold(exe, dst) {
		if err := os.MkdirAll(setupDstDir, 0o755); err != nil {
			return err
		}
		if err := copyFile(exe, dst); err != nil {
			return fmt.Errorf("Binary nach %s kopieren: %w", dst, err)
		}
		fmt.Println("Binary kopiert nach", dst)
	}
	// Konto geaendert (z. B. NT SERVICE -> LocalSystem)? Dann neu anlegen - das Konto laesst sich nur beim Anlegen setzen.
	if exists {
		if acct, err := serviceAccount(name); err == nil && !sameAccount(acct, cfg.Install.Account) {
			fmt.Printf("Dienstkonto ist %q, Konfiguration will %q -> Dienst wird neu angelegt (Identitaet und Logs bleiben)\n", acct, firstNonEmpty(cfg.Install.Account, "LocalSystem"))
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
			fmt.Println("  GPU-Z-Relay-Aufgabe:", err, "(ohne Relay laufen NVML-Sensoren weiter)")
		} else {
			fmt.Println("  GPU-Z-Relay-Aufgabe angelegt (bei Anmeldung, ohne Adminrechte)")
		}
	}
	if old := `C:\Program Files\ollama-router-agent`; !strings.EqualFold(old, setupDstDir) {
		if _, err := os.Stat(old); err == nil {
			if err := os.RemoveAll(old); err == nil {
				fmt.Println("Alter Programmordner entfernt:", old)
			}
		}
	}
	fmt.Println("\nWarte auf den Dienst ...")
	time.Sleep(6 * time.Second)
	printHealth(cfg.Health.Listen)
	fmt.Println("\nNaechster Schritt: Router-UI -> Tab Agenten -> diesen Rechner freigeben (Fingerprint siehe oben).")
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
	fmt.Println("Alte Installation gefunden - Umzug nach skirnir-agent (Dienst, Verzeichnis, Aufgabe) ...")
	_ = controlService(legacyServiceName, "stop")
	stopRelayProcesses()
	runQuiet("schtasks.exe", "/Delete", "/F", "/TN", legacyRelayTaskName)
	oldDir, newDir := filepath.Dir(legacyConfigPathWindows), filepath.Dir(newDefault)
	if err := os.Rename(oldDir, newDir); err != nil {
		return false, fmt.Errorf("%s nach %s verschieben: %w", oldDir, newDir, err)
	}
	raw, err := os.ReadFile(newDefault)
	if err != nil {
		return false, err
	}
	s := string(raw)
	for old, repl := range map[string]string{`\ollama-router-agent\`: `\skirnir-agent\`, "OllamaRouterAgent": "SkirnirAgent", "Ollama Router Agent": "Skirnir Agent"} {
		s = strings.ReplaceAll(s, old, repl)
	}
	_ = copyFile(newDefault, newDefault+".vor-umzug-"+time.Now().Format("20060102-150405"))
	if err := os.WriteFile(newDefault, []byte(s), 0o600); err != nil {
		return false, err
	}
	c := &Config{}
	c.Install.ServiceName = legacyServiceName
	c.applyDefaults()
	if err := uninstall(c); err != nil {
		fmt.Println("  alter Dienst:", err)
	}
	*cfgPath = newDefault
	return true, nil
}

func setupWriteConfig(in *bufio.Reader, cfgPath string) error {
	fmt.Println("Noch keine Konfiguration - drei Fragen:")
	url := ""
	for url == "" {
		url = strings.TrimRight(ask(in, "Adresse des Routers (z. B. https://router.example.net:11435)", ""), "/")
		if !strings.HasPrefix(url, "https://") && !strings.HasPrefix(url, "http://") {
			fmt.Println("  bitte mit https:// angeben")
			url = ""
		}
	}
	key := ask(in, "Oeffentlicher Update-Schluessel des Betreibers (base64; leer = Selbst-Update spaeter eintragen)", "")
	user := ask(in, "Benutzer, der den Dienst ohne Adminrechte steuern darf", os.Getenv("COMPUTERNAME")+`\`+os.Getenv("USERNAME"))
	var b strings.Builder
	fmt.Fprintf(&b, "# Skirnir Agent - angelegt von `skirnir-agent.exe setup` am %s\n", time.Now().Format("2006-01-02 15:04"))
	b.WriteString("# Alle Schluessel: config.example.yaml im Repo (github.com/psewar/skirnir, agent/)\n\n")
	fmt.Fprintf(&b, "router:\n  url: %s\n\ntunnel:\n  upstream: http://127.0.0.1:11434\n\n", url)
	if key != "" {
		fmt.Fprintf(&b, "update:\n  public_key: %s\n\n", key)
	} else {
		b.WriteString("# update:\n#   public_key: <base64>   # Selbst-Update ueber den Router: Schluessel vom Betreiber (deploy.py --agent)\n\n")
	}
	b.WriteString("install:\n  service_name: SkirnirAgent\n  display_name: Skirnir Agent\n  account: ''   # LocalSystem: GPU-Schutz (Power-Limit) und GPU-Z direkt\n")
	fmt.Fprintf(&b, "  allow_control_users:\n    - '%s'\n", user)
	if err := os.MkdirAll(filepath.Dir(cfgPath), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(cfgPath, []byte(b.String()), 0o600); err != nil {
		return err
	}
	fmt.Println("Konfiguration geschrieben:", cfgPath)
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
		if key := ask(in, "Update-Schluessel des Betreibers fehlt (update.public_key) - jetzt eintragen (leer = spaeter)", ""); key != "" {
			s = strings.TrimRight(s, "\r\n") + "\n\n# Selbst-Update ueber den Router: Manifest muss mit dem Betreiber-Schluessel signiert sein\nupdate:\n  public_key: " + key + "\n"
			changed = true
		}
	}
	if strings.Contains(s, "NT SERVICE") {
		if strings.EqualFold(ask(in, "Dienstkonto steht auf NT SERVICE (kein GPU-Schutz). Auf LocalSystem umstellen? (j/n)", "j"), "j") {
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
		fmt.Println("  Hinweis: der Block `mimir:` heisst seit 0.9.1 `secret_store:` - bitte von Hand umbenennen, sonst scheitert der Start.")
	}
	if changed {
		_ = copyFile(cfgPath, cfgPath+".bak-"+time.Now().Format("20060102-150405"))
		if err := os.WriteFile(cfgPath, []byte(s), 0o600); err != nil {
			return err
		}
		fmt.Println("Konfiguration aktualisiert (Sicherung daneben)")
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
