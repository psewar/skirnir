//go:build windows

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
	"golang.org/x/sys/windows/svc/mgr"
)

// install legt den Dienst an: Konto, Recovery, Event-Log-Quelle, ACLs, Firewall, Steuerrecht fuer Benutzer.
// Braucht einmal Admin. Idempotent genug, um nach einem Fehler erneut zu laufen.
func install(cfg *Config, cfgPath string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	exe, _ = filepath.Abs(exe)
	in := cfg.Install
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("SCM: %w (Admin-Rechte?)", err)
	}
	defer m.Disconnect()

	// Verzeichnisse
	for _, d := range []string{filepath.Dir(cfgPath), cfg.Logging.Dir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return err
		}
	}
	// Event-Log-Quelle
	if err := eventlog.InstallAsEventCreate(in.ServiceName, eventlog.Info|eventlog.Warning|eventlog.Error); err != nil && !strings.Contains(err.Error(), "already exists") {
		fmt.Printf("  Event-Log-Quelle: %v (weiter)\n", err)
	}
	// Dienst
	if s, err := m.OpenService(in.ServiceName); err == nil {
		s.Close()
		return fmt.Errorf("Dienst %s existiert schon (erst 'uninstall')", in.ServiceName)
	}
	conf := mgr.Config{
		DisplayName:      in.DisplayName,
		Description:      in.Description,
		StartType:        mgr.StartAutomatic,
		DelayedAutoStart: true,
		ServiceStartName: in.Account, // "" = LocalSystem; "NT SERVICE\<Name>" = virtuelles Konto, legt der SCM selbst an
	}
	s, err := m.CreateService(in.ServiceName, exe, conf, "--config", cfgPath)
	if err != nil {
		return fmt.Errorf("CreateService: %w", err)
	}
	defer s.Close()
	fmt.Printf("  Dienst %s angelegt: %s --config %s (Konto: %s)\n", in.ServiceName, exe, cfgPath, firstNonEmpty(in.Account, "LocalSystem"))
	if err := s.SetRecoveryActions([]mgr.RecoveryAction{
		{Type: mgr.ServiceRestart, Delay: 5 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 30 * time.Second},
		{Type: mgr.ServiceRestart, Delay: 60 * time.Second},
	}, 86400); err != nil {
		fmt.Printf("  Recovery: %v\n", err)
	}
	_ = s.SetRecoveryActionsOnNonCrashFailures(true)

	return applyRules(cfg, cfgPath)
}

// applyRules setzt Verzeichnis-ACLs, Firewall-Regeln und das Steuerrecht - idempotent, auch bei Updates (Verb apply-rules).
func applyRules(cfg *Config, cfgPath string) error {
	in := cfg.Install
	// Config-Verzeichnis abschotten: enthaelt Router-Token und Secret-Store-Client-Secret. Vererbung kappen,
	// nur SYSTEM, Administratoren, das Dienstkonto und die steuernden Benutzer duerfen hinein.
	acl := []string{filepath.Dir(cfgPath), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"}
	if in.Account != "" {
		acl = append(acl, fmt.Sprintf("%s:(OI)(CI)M", in.Account))
	}
	for _, u := range in.AllowControlUsers {
		acl = append(acl, fmt.Sprintf("%s:(OI)(CI)M", u))
	}
	if out, err := exec.Command("icacls.exe", acl...).CombinedOutput(); err != nil {
		fmt.Printf("  ACL %s: %v %s\n", filepath.Dir(cfgPath), err, strings.TrimSpace(string(out)))
	} else {
		fmt.Printf("  %s abgeschottet (SYSTEM, Administratoren, %s, %s)\n", filepath.Dir(cfgPath), firstNonEmpty(in.Account, "-"), strings.Join(in.AllowControlUsers, ", "))
	}
	// Dateirechte fuer das Dienstkonto
	if in.Account != "" {
		for _, p := range in.GrantRead {
			runQuiet("icacls.exe", p, "/grant", fmt.Sprintf("%s:(OI)(CI)RX", in.Account))
			fmt.Printf("  Lesen fuer %s: %s\n", in.Account, p)
		}
		for _, p := range append([]string{cfg.Logging.Dir}, in.GrantModify...) {
			_ = os.MkdirAll(p, 0o755)
			runQuiet("icacls.exe", p, "/grant", fmt.Sprintf("%s:(OI)(CI)M", in.Account))
			fmt.Printf("  Aendern fuer %s: %s\n", in.Account, p)
		}
	}
	// Regel der TLS-Vorschaltstelle entfernen, wenn sie abgeschaltet ist (Tunnel braucht keine eingehende Regel)
	if !cfg.OllamaProxy.on() {
		runQuiet("netsh.exe", "advfirewall", "firewall", "delete", "rule", "name=Ollama TLS-Proxy (ollama-router-agent)")
	}
	// Firewall (eingehend) fuer Kinder, die im LAN lauschen
	for _, f := range in.Firewall {
		runQuiet("netsh.exe", "advfirewall", "firewall", "delete", "rule", "name="+f.Name)
		args := []string{"advfirewall", "firewall", "add", "rule", "name=" + f.Name, "dir=in", "action=allow", "protocol=TCP",
			fmt.Sprintf("localport=%d", f.Port), "profile=any"}
		if len(f.Remote) > 0 {
			args = append(args, "remoteip="+strings.Join(f.Remote, ","))
		}
		if f.Program != "" {
			args = append(args, "program="+f.Program)
		}
		if out, err := exec.Command("netsh.exe", args...).CombinedOutput(); err != nil {
			fmt.Printf("  Firewall %s: %v %s\n", f.Name, err, strings.TrimSpace(string(out)))
		} else {
			fmt.Printf("  Firewall-Regel %s (TCP %d, von %s)\n", f.Name, f.Port, firstNonEmpty(strings.Join(f.Remote, ","), "ueberall"))
		}
	}
	// Start/Stopp ohne UAC fuer die genannten Benutzer
	for _, u := range in.AllowControlUsers {
		if err := allowServiceControl(in.ServiceName, u); err != nil {
			fmt.Printf("  Steuerrecht %s: %v\n", u, err)
		} else {
			fmt.Printf("  %s darf den Dienst starten/stoppen\n", u)
		}
	}
	return nil
}

// allowServiceControl haengt eine ACE (Start, Stopp, Abfrage) fuer den Benutzer an die Dienst-SDDL.
func allowServiceControl(service, user string) error {
	sid, _, _, err := windows.LookupSID("", user)
	if err != nil {
		return fmt.Errorf("LookupSID %s: %w", user, err)
	}
	out, err := exec.Command("sc.exe", "sdshow", service).Output()
	if err != nil {
		return fmt.Errorf("sc sdshow: %w", err)
	}
	sddl := strings.TrimSpace(string(out))
	ace := fmt.Sprintf("(A;;CCLCSWRPWPDTLOCRRC;;;%s)", sid.String())
	if strings.Contains(sddl, ace) {
		return nil
	}
	if i := strings.Index(sddl, "S:"); i >= 0 {
		sddl = sddl[:i] + ace + sddl[i:]
	} else {
		sddl += ace
	}
	if out, err := exec.Command("sc.exe", "sdset", service, sddl).CombinedOutput(); err != nil {
		return fmt.Errorf("sc sdset: %v %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func uninstall(cfg *Config) error {
	in := cfg.Install
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("SCM: %w (Admin-Rechte?)", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(in.ServiceName)
	if err != nil {
		return fmt.Errorf("Dienst %s nicht gefunden", in.ServiceName)
	}
	defer s.Close()
	if st, err := s.Query(); err == nil && st.State != svc.Stopped {
		fmt.Println("  stoppe Dienst ...")
		_, _ = s.Control(svc.Stop)
		waitState(s, svc.Stopped, 30*time.Second)
	}
	if err := s.Delete(); err != nil {
		return fmt.Errorf("Delete: %w", err)
	}
	_ = eventlog.Remove(in.ServiceName)
	for _, f := range in.Firewall {
		runQuiet("netsh.exe", "advfirewall", "firewall", "delete", "rule", "name="+f.Name)
	}
	fmt.Printf("  Dienst %s entfernt (Config, Logs und ACLs bleiben)\n", in.ServiceName)
	return nil
}

// openServiceLimited oeffnet den Dienst mit genau den Rechten, die das Verb braucht. mgr.Connect() verlangt
// SC_MANAGER_ALL_ACCESS und scheitert ohne Admin, obwohl die Dienst-SDDL dem Benutzer Start/Stopp erlaubt.
func openServiceLimited(name string, access uint32) (*mgr.Service, func(), error) {
	h, err := windows.OpenSCManager(nil, nil, windows.SC_MANAGER_CONNECT)
	if err != nil {
		return nil, nil, fmt.Errorf("SCM: %w", err)
	}
	sh, err := windows.OpenService(h, windows.StringToUTF16Ptr(name), access)
	if err != nil {
		windows.CloseServiceHandle(h)
		return nil, nil, fmt.Errorf("Dienst %s: %w", name, err)
	}
	s := &mgr.Service{Name: name, Handle: sh}
	return s, func() { s.Close(); windows.CloseServiceHandle(h) }, nil
}

func controlService(name string, cmd string) error {
	s, done, err := openServiceLimited(name, windows.SERVICE_QUERY_STATUS|windows.SERVICE_START|windows.SERVICE_STOP)
	if err != nil {
		return err
	}
	defer done()
	switch cmd {
	case "start":
		if err := s.Start(); err != nil {
			return err
		}
		return waitState(s, svc.Running, 60*time.Second)
	case "stop":
		if _, err := s.Control(svc.Stop); err != nil {
			return err
		}
		return waitState(s, svc.Stopped, 30*time.Second)
	case "restart":
		if st, _ := s.Query(); st.State != svc.Stopped {
			if _, err := s.Control(svc.Stop); err != nil {
				return err
			}
			if err := waitState(s, svc.Stopped, 30*time.Second); err != nil {
				return err
			}
		}
		if err := s.Start(); err != nil {
			return err
		}
		return waitState(s, svc.Running, 60*time.Second)
	}
	return fmt.Errorf("unbekannt: %s", cmd)
}

func serviceState(name string) (string, error) {
	s, done, err := openServiceLimited(name, windows.SERVICE_QUERY_STATUS)
	if err != nil {
		if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) {
			return "nicht installiert", nil
		}
		return "", err
	}
	defer done()
	st, err := s.Query()
	if err != nil {
		return "", err
	}
	names := map[svc.State]string{svc.Stopped: "gestoppt", svc.StartPending: "startet", svc.StopPending: "stoppt", svc.Running: "laeuft", svc.Paused: "pausiert"}
	if n, ok := names[st.State]; ok {
		return fmt.Sprintf("%s (PID %d)", n, st.ProcessId), nil
	}
	return fmt.Sprintf("Zustand %d", st.State), nil
}

func waitState(s *mgr.Service, want svc.State, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		st, err := s.Query()
		if err != nil {
			return err
		}
		if st.State == want {
			return nil
		}
		time.Sleep(300 * time.Millisecond)
	}
	return fmt.Errorf("Dienst hat Zustand %d nicht in %s erreicht", want, timeout)
}

func runQuiet(name string, args ...string) {
	c := exec.Command(name, args...)
	c.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	_ = c.Run()
}
