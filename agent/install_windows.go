//go:build windows

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
	"golang.org/x/sys/windows/svc/mgr"
)

// serviceNotInstalled is the serviceState when the service does not exist; setup compares against this.
const serviceNotInstalled = "not installed"

// install creates the service: account, recovery, event log source, ACLs, firewall, and user control rights.
// Requires admin once. Idempotent enough to re-run after an error.
func install(cfg *Config, cfgPath string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	exe, _ = filepath.Abs(exe)
	if abs, err := filepath.Abs(cfgPath); err == nil {
		cfgPath = abs // A relative path would be literal in the SCM; the service starts from System32 and would not find it.
	}
	in := cfg.Install
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("SCM: %w (admin rights?)", err)
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
		fmt.Printf("  event log source: %v (continuing)\n", err)
	}
	// Dienst
	if s, err := m.OpenService(in.ServiceName); err == nil {
		s.Close()
		return fmt.Errorf("service %s already exists (run 'uninstall' first)", in.ServiceName)
	}
	conf := mgr.Config{
		DisplayName:      in.DisplayName,
		Description:      in.Description,
		StartType:        mgr.StartAutomatic,
		DelayedAutoStart: true,
		ServiceStartName: in.Account, // "" = LocalSystem; "NT SERVICE\<Name>" = virtual account, created by the SCM itself.
	}
	s, err := m.CreateService(in.ServiceName, exe, conf, "--config", cfgPath)
	if err != nil {
		return fmt.Errorf("CreateService: %w", err)
	}
	defer s.Close()
	fmt.Printf("  service %s created: %s --config %s (account: %s)\n", in.ServiceName, exe, cfgPath, firstNonEmpty(in.Account, "LocalSystem"))
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

// applyRules sets directory ACLs, firewall rules, and control rights - idempotent, even on updates (apply-rules verb).
func applyRules(cfg *Config, cfgPath string) error {
	in := cfg.Install
	// Isolate the Config directory: contains router token, secret store client secret, identity key, and
	// provisioning (MQTT password). Cut inheritance; only SYSTEM, Administrators, and the service account may access it.
	// Controlling users (allow_control_users) get ONLY the service control right (below), no directory rights:
	// With write access, they could rewrite children[].cmd and execute it as SYSTEM via restart (Review 2026-09-25).
	acl := []string{filepath.Dir(cfgPath), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"}
	if in.Account != "" {
		acl = append(acl, fmt.Sprintf("%s:(OI)(CI)M", in.Account))
	}
	for _, u := range in.AllowControlUsers {
		runQuiet("icacls.exe", filepath.Dir(cfgPath), "/remove:g", u) // Leftover from pre-0.9.0 installations
	}
	if out, err := exec.Command("icacls.exe", acl...).CombinedOutput(); err != nil {
		fmt.Printf("  ACL %s: %v %s\n", filepath.Dir(cfgPath), err, strings.TrimSpace(string(out)))
	} else {
		fmt.Printf("  %s locked down (SYSTEM, Administrators, %s)\n", filepath.Dir(cfgPath), firstNonEmpty(in.Account, "-"))
	}
	// File rights for the service account
	if in.Account != "" {
		for _, p := range in.GrantRead {
			runQuiet("icacls.exe", p, "/grant", fmt.Sprintf("%s:(OI)(CI)RX", in.Account))
			fmt.Printf("  read for %s: %s\n", in.Account, p)
		}
		for _, p := range append([]string{cfg.Logging.Dir}, in.GrantModify...) {
			_ = os.MkdirAll(p, 0o755)
			runQuiet("icacls.exe", p, "/grant", fmt.Sprintf("%s:(OI)(CI)M", in.Account))
			fmt.Printf("  modify for %s: %s\n", in.Account, p)
		}
	}
	// Remove the TLS front-end rule if disabled (tunnel needs no inbound rule)
	if !cfg.OllamaProxy.on() {
		runQuiet("netsh.exe", "advfirewall", "firewall", "delete", "rule", "name=Ollama TLS-Proxy (skirnir-agent)")
		runQuiet("netsh.exe", "advfirewall", "firewall", "delete", "rule", "name=Ollama TLS-Proxy (ollama-router-agent)")
	}
	// Firewall (inbound) for children listening on the LAN
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
			fmt.Printf("  firewall rule %s (TCP %d, from %s)\n", f.Name, f.Port, firstNonEmpty(strings.Join(f.Remote, ","), "anywhere"))
		}
	}
	// Update display name and description (since 0.10.0 "Skirnir Agent"; the account remains, only install can set it)
	if s, done, err := openServiceLimited(in.ServiceName, windows.SERVICE_QUERY_CONFIG|windows.SERVICE_CHANGE_CONFIG); err == nil {
		if c, err := s.Config(); err == nil && (c.DisplayName != in.DisplayName || c.Description != in.Description) {
			c.DisplayName, c.Description = in.DisplayName, in.Description
			if err := s.UpdateConfig(c); err == nil {
				fmt.Printf("  display name: %s\n", in.DisplayName)
			}
		}
		done()
	}
	// Start/Stop without UAC for the specified users
	for _, u := range in.AllowControlUsers {
		if err := allowServiceControl(in.ServiceName, u); err != nil {
			fmt.Printf("  control right %s: %v\n", u, err)
		} else {
			fmt.Printf("  %s may start/stop the service\n", u)
		}
	}
	return nil
}

// allowServiceControl appends an ACE (Start, Stop, Query) for the user to the service SDDL.
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
		return fmt.Errorf("SCM: %w (admin rights?)", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(in.ServiceName)
	if err != nil {
		return fmt.Errorf("service %s not found", in.ServiceName)
	}
	defer s.Close()
	if st, err := s.Query(); err == nil && st.State != svc.Stopped {
		fmt.Println("  stopping service ...")
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
	fmt.Printf("  service %s removed (config, logs and ACLs stay)\n", in.ServiceName)
	return nil
}

// openServiceLimited opens the service with exactly the rights the verb needs. mgr.Connect() requires
// SC_MANAGER_ALL_ACCESS and fails without admin, even if the service SDDL allows the user Start/Stop.
func openServiceLimited(name string, access uint32) (*mgr.Service, func(), error) {
	h, err := windows.OpenSCManager(nil, nil, windows.SC_MANAGER_CONNECT)
	if err != nil {
		return nil, nil, fmt.Errorf("SCM: %w", err)
	}
	sh, err := windows.OpenService(h, windows.StringToUTF16Ptr(name), access)
	if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) && name != legacyServiceName {
		// Rename 0.11.0: on a not-yet-migrated node, the service is still named OllamaRouterAgent
		if sh2, err2 := windows.OpenService(h, windows.StringToUTF16Ptr(legacyServiceName), access); err2 == nil {
			sh, err, name = sh2, nil, legacyServiceName
		}
	}
	if err != nil {
		windows.CloseServiceHandle(h)
		return nil, nil, fmt.Errorf("service %s: %w", name, err)
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
	return fmt.Errorf("unknown: %s", cmd)
}

// serviceAccount: account per SCM ("" or LocalSystem = LocalSystem).
func serviceAccount(name string) (string, error) {
	s, done, err := openServiceLimited(name, windows.SERVICE_QUERY_CONFIG)
	if err != nil {
		return "", err
	}
	defer done()
	c, err := s.Config()
	if err != nil {
		return "", err
	}
	return c.ServiceStartName, nil
}

// sameAccount compares service accounts; empty and LocalSystem are the same.
func sameAccount(a, b string) bool {
	norm := func(x string) string {
		x = strings.ToLower(strings.TrimSpace(x))
		if x == "" || x == "localsystem" || strings.HasSuffix(x, "localsystem") {
			return ""
		}
		return x
	}
	return norm(a) == norm(b)
}

func serviceState(name string) (string, error) {
	s, done, err := openServiceLimited(name, windows.SERVICE_QUERY_STATUS)
	if err != nil {
		if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) {
			return serviceNotInstalled, nil
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
	return fmt.Sprintf("state %d", st.State), nil
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
	return fmt.Errorf("service did not reach state %d within %s", want, timeout)
}

func runQuiet(name string, args ...string) {
	c := exec.Command(name, args...)
	hideWindow(c)
	_ = c.Run()
}
