package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

var (
	probeAdvapi            = windows.NewLazySystemDLL("advapi32.dll")
	pCreateRestrictedToken = probeAdvapi.NewProc("CreateRestrictedToken")
	pLogonUserW            = probeAdvapi.NewProc("LogonUserW")
)

const (
	disableMaxPrivilege    = 0x1
	logon32LogonService    = 5
	logon32ProviderDefault = 0
	probeTask              = "SkirnirProbeIdentity"
)

func tokenUser(t windows.Token) (sid string, name string) {
	u, err := t.GetTokenUser()
	if err != nil {
		return "", "?"
	}
	sid = u.User.Sid.String()
	if acc, dom, _, err := u.User.Sid.LookupAccount(""); err == nil {
		return sid, dom + `\` + acc
	}
	return sid, sid
}

// probeToken creates a primary token of the given kind from this process's token (see probe_identity.go).
func probeToken(kind string) (windows.Token, error) {
	var cur windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_DUPLICATE|windows.TOKEN_QUERY|
		windows.TOKEN_ASSIGN_PRIMARY|windows.TOKEN_ADJUST_DEFAULT, &cur); err != nil {
		return 0, fmt.Errorf("OpenProcessToken: %w", err)
	}
	defer cur.Close()
	var tok windows.Token
	switch kind {
	case "system":
		if err := windows.DuplicateTokenEx(cur, windows.MAXIMUM_ALLOWED, nil, windows.SecurityImpersonation,
			windows.TokenPrimary, &tok); err != nil {
			return 0, fmt.Errorf("DuplicateTokenEx: %w", err)
		}
	case "restricted":
		admins, err := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
		if err != nil {
			return 0, err
		}
		u, err := cur.GetTokenUser()
		if err != nil {
			return 0, err
		}
		disable := []windows.SIDAndAttributes{{Sid: admins}, {Sid: u.User.Sid}}
		r, _, e := pCreateRestrictedToken.Call(uintptr(cur), disableMaxPrivilege, uintptr(len(disable)),
			uintptr(unsafe.Pointer(&disable[0])), 0, 0, 0, 0, uintptr(unsafe.Pointer(&tok)))
		if r == 0 {
			return 0, fmt.Errorf("CreateRestrictedToken: %v", e)
		}
		medium, err := windows.StringToSid("S-1-16-8192")
		if err != nil {
			tok.Close()
			return 0, err
		}
		tml := windows.Tokenmandatorylabel{Label: windows.SIDAndAttributes{Sid: medium, Attributes: windows.SE_GROUP_INTEGRITY}}
		if err := windows.SetTokenInformation(tok, windows.TokenIntegrityLevel, (*byte)(unsafe.Pointer(&tml)), tml.Size()); err != nil {
			tok.Close()
			return 0, fmt.Errorf("integrity level: %w", err)
		}
	case "localservice":
		r, _, e := pLogonUserW.Call(uintptr(unsafe.Pointer(windows.StringToUTF16Ptr("LocalService"))),
			uintptr(unsafe.Pointer(windows.StringToUTF16Ptr("NT AUTHORITY"))), 0, logon32LogonService, logon32ProviderDefault,
			uintptr(unsafe.Pointer(&tok)))
		if r == 0 {
			return 0, fmt.Errorf("LogonUser LocalService: %v", e)
		}
	default:
		return 0, fmt.Errorf("unknown token %q", kind)
	}
	return tok, nil
}

// tailWriter keeps the last lines of a child's output.
type tailWriter struct {
	mu    sync.Mutex
	buf   bytes.Buffer
	lines []string
	max   int
}

func (w *tailWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf.Write(p)
	for {
		data := w.buf.Bytes()
		i := bytes.IndexByte(data, '\n')
		if i < 0 { // no full line yet: the rest waits for the next write
			break
		}
		w.lines = append(w.lines, strings.TrimRight(string(data[:i]), "\r"))
		w.buf.Next(i + 1)
		if len(w.lines) > w.max {
			w.lines = w.lines[len(w.lines)-w.max:]
		}
	}
	return len(p), nil
}

func (w *tailWriter) all() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]string(nil), w.lines...)
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}

// probeChild starts one child under one token on a port of its own and measures it.
func probeChild(sp ChildSpec, kind string, tok windows.Token, grace time.Duration) ProbeChildResult {
	res := ProbeChildResult{Child: sp.Name, Token: kind, CUDA: "not seen"}
	port, err := freePort()
	if err != nil {
		res.StartErr = "no free port: " + err.Error()
		return res
	}
	p := probeSpec(sp, port)
	res.Port = port
	env := os.Environ()
	if kind != "system" { // the token's own profile and variables, as a service under that account would get them
		if e, err := tok.Environ(false); err == nil {
			env = e
		}
	}
	group, err := newChildGroup()
	if err != nil {
		res.StartErr = err.Error()
		return res
	}
	defer windows.CloseHandle(group.job)
	out := &tailWriter{max: 400}
	cmd := exec.Command(p.Cmd, p.Args...)
	cmd.Dir = p.Cwd
	cmd.Env = append(env, p.Env...)
	cmd.Stdout, cmd.Stderr = out, out
	cmd.SysProcAttr = &syscall.SysProcAttr{Token: syscall.Token(tok), HideWindow: true, CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP}
	start := time.Now()
	if err := cmd.Start(); err != nil {
		res.StartErr = err.Error()
		return res
	}
	res.Started = true
	tree, err := group.track(cmd.Process.Pid)
	if err != nil {
		res.StartErr = "job object: " + err.Error()
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	deadline := time.Now().Add(min(grace, time.Duration(sp.StartupGraceS*float64(time.Second))+30*time.Second))
	ended := false
	for time.Now().Before(deadline) && !res.Healthy && !ended {
		select {
		case <-done:
			ended = true
			code := cmd.ProcessState.ExitCode()
			res.ExitCode = &code
		case <-time.After(2 * time.Second):
			if c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), time.Second); err == nil {
				c.Close()
				res.Healthy = true
				res.HealthyS = time.Since(start).Seconds()
			}
		}
	}
	if res.Healthy {
		time.Sleep(5 * time.Second) // let it settle its GPU context
		if vram, err := gpuProcessVRAM(); err == nil && tree != nil {
			var sum uint64
			for _, pid := range tree.pids() {
				sum += vram[pid]
			}
			res.VRAMMiB = sum / (1 << 20)
		}
	}
	if tree != nil {
		tree.kill()
	}
	if !ended {
		select {
		case <-done:
		case <-time.After(20 * time.Second):
		}
	}
	res.TreeEnded = tree != nil && tree.alive() == 0
	if tree != nil {
		tree.release()
	}
	lines := out.all()
	res.CUDA = cudaInOutput(lines)
	if !res.Healthy || strings.HasPrefix(res.CUDA, "error") {
		if len(lines) > 25 {
			lines = lines[len(lines)-25:]
		}
		res.LogTail = lines
	}
	return res
}

// probeRightsHere checks the agent's privileged functions with this process's own rights (run under each token).
func probeRightsHere() ProbeRights {
	_, name := func() (string, string) {
		var t windows.Token
		if windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_QUERY, &t) != nil {
			return "", "?"
		}
		defer t.Close()
		return tokenUser(t)
	}()
	r := ProbeRights{Token: name}
	if d, err := openNVML(); err != nil {
		r.NVMLSet = "nvml not available: " + err.Error()
	} else {
		lim := d.limits()
		if !lim.OK {
			r.NVMLSet = "limits not readable: " + lim.Err
		} else if err := d.setLimit(lim.Cur); err != nil {
			r.NVMLSet = "denied: " + err.Error()
		} else {
			r.NVMLSet = fmt.Sprintf("allowed (set to the current %.0f W)", lim.Cur)
		}
		d.close()
	}
	procs := listProcesses()
	gpuzRuns, withPath := false, 0
	for _, p := range procs {
		gpuzRuns = gpuzRuns || strings.EqualFold(p.Name, "GPU-Z.exe") || strings.HasPrefix(strings.ToLower(p.Name), "gpu-z")
		if p.Path != "" {
			withPath++
		}
	}
	r.ProcPaths = fmt.Sprintf("%d of %d", withPath, len(procs))
	gz := newGPUZReader(nil)
	if err := gz.open(); err != nil {
		r.GPUZ = "not readable: " + err.Error()
		if !gpuzRuns {
			r.GPUZ = "GPU-Z not running (no answer possible)"
		}
	} else {
		r.GPUZ = "readable"
		gz.drop()
	}
	sids, err := registry.USERS.ReadSubKeyNames(-1)
	if err != nil {
		r.UserHives = "HKEY_USERS not listable: " + err.Error()
	} else {
		n, ok := 0, 0
		for _, sid := range sids {
			if !strings.HasPrefix(sid, "S-1-5-21-") || strings.HasSuffix(sid, "_Classes") {
				continue
			}
			n++
			if k, err := registry.OpenKey(registry.USERS, sid+`\`+uninstallKey, registry.ENUMERATE_SUB_KEYS); err == nil {
				k.Close()
				ok++
			}
		}
		r.UserHives = fmt.Sprintf("%d of %d user hives readable", ok, n)
	}
	return r
}

// probeRightsAs runs this program's rights check under tok and reads its JSON.
func probeRightsAs(kind string, tok windows.Token, cfgPath string) ProbeRights {
	exe, err := os.Executable()
	if err != nil {
		return ProbeRights{Token: kind, Err: err.Error()}
	}
	cmd := exec.Command(exe, "probe-identity", "--inner-rights", "--config", cfgPath)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Token: syscall.Token(tok), HideWindow: true}
	if kind != "system" {
		if e, err := tok.Environ(false); err == nil {
			cmd.Env = e
		}
	}
	if err := cmd.Run(); err != nil {
		return ProbeRights{Token: kind, Err: fmt.Sprintf("rights check did not run: %v %s", err, strings.TrimSpace(stderr.String()))}
	}
	var r ProbeRights
	if err := json.Unmarshal(stdout.Bytes(), &r); err != nil {
		return ProbeRights{Token: kind, Err: "rights check output unreadable: " + strings.TrimSpace(stdout.String())}
	}
	r.Token = kind + " (" + r.Token + ")"
	return r
}

// runProbeIdentity is the verb. As LocalSystem it measures; as an administrator it runs itself once as LocalSystem
// (scheduled task) and prints that report; otherwise it refuses with the next step.
func runProbeIdentity(cfg *Config, cfgPath string, o ProbeOptions, out io.Writer) error {
	if o.InnerRights {
		return json.NewEncoder(out).Encode(probeRightsHere())
	}
	var self windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_QUERY, &self); err != nil {
		return err
	}
	sid, name := tokenUser(self)
	elevated := self.IsElevated()
	self.Close()
	if sid != "S-1-5-18" {
		if !elevated {
			return fmt.Errorf("administrator rights needed (the probe then runs itself once as LocalSystem)")
		}
		return probeViaSystemTask(cfgPath, o, out)
	}
	rep := ProbeReport{Node: cfg.Node, AgentVersion: version, RunningAs: name, At: time.Now(), Tokens: map[string]string{}}
	run, skipped := probeChildren(cfg, o)
	rep.Skipped = skipped
	for _, kind := range o.Tokens {
		tok, err := probeToken(kind)
		if err != nil {
			rep.Tokens[kind] = "not created: " + err.Error()
			continue
		}
		rep.Tokens[kind] = "ok"
		for _, sp := range run {
			fmt.Fprintf(out, "probing child %s as %s ...\n", sp.Name, kind)
			rep.Children = append(rep.Children, probeChild(sp, kind, tok, o.Grace))
		}
		rep.Rights = append(rep.Rights, probeRightsAs(kind, tok, cfgPath))
		tok.Close()
	}
	rep.print(out)
	if o.Out != "" {
		tmp := o.Out + ".tmp"
		if err := os.WriteFile(tmp, rep.json(), 0o600); err != nil {
			return err
		}
		if err := os.Rename(tmp, o.Out); err != nil {
			return err
		}
		fmt.Fprintln(out, "report:", o.Out)
	}
	return nil
}

// probeViaSystemTask: the measurement needs LocalSystem (it creates tokens for other accounts and starts processes with
// them). The children it starts then run this program's tokens, so it runs only from a folder no one else can change.
func probeViaSystemTask(cfgPath string, o ProbeOptions, out io.Writer) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	if who, err := exposureOfProgram(exe); err != nil {
		return err
	} else if who != "" {
		return fmt.Errorf("%s can be changed by %s; copy it into a folder only administrators can change "+
			"(for example C:\\Program Files\\skirnir-agent\\probe) and run it from there", exe, who)
	}
	dir := filepath.Join(filepath.Dir(cfgPath), "probe")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	report := filepath.Join(dir, "probe-identity-"+time.Now().Format("20060102-150405")+".json")
	args := []string{"probe-identity", "--config", cfgPath, "--out", report, "--grace-s", fmt.Sprint(int(o.Grace.Seconds()))}
	if len(o.Children) > 0 {
		args = append(args, "--child", strings.Join(o.Children, ","))
	}
	args = append(args, "--token", strings.Join(o.Tokens, ","))
	if o.WithUpstream != "" {
		args = append(args, "--with-upstream", o.WithUpstream)
	}
	quoted := make([]string, len(args))
	for i, a := range args {
		quoted[i] = syscall.EscapeArg(a)
	}
	ps := fmt.Sprintf(`$a = New-ScheduledTaskAction -Execute %s -Argument %s; `+
		`Register-ScheduledTask -TaskName %s -Action $a -User 'NT AUTHORITY\SYSTEM' -RunLevel Highest -Force | Out-Null; `+
		`Start-ScheduledTask -TaskName %s`,
		psQuote(exe), psQuote(strings.Join(quoted, " ")), probeTask, probeTask)
	if o2, err := exec.Command("powershell.exe", "-NoProfile", "-Command", ps).CombinedOutput(); err != nil {
		return fmt.Errorf("scheduled task: %v %s", err, strings.TrimSpace(string(o2)))
	}
	defer exec.Command("powershell.exe", "-NoProfile", "-Command", "Unregister-ScheduledTask -TaskName "+probeTask+" -Confirm:$false").Run()
	fmt.Fprintf(out, "running as LocalSystem (task %s); report will be %s\n", probeTask, report)
	wait := time.Duration(len(o.Tokens)) * (o.Grace + 90*time.Second) * 4
	limit := time.Now().Add(wait)
	for time.Now().Before(limit) {
		if b, err := os.ReadFile(report); err == nil {
			var rep ProbeReport
			if err := json.Unmarshal(b, &rep); err != nil {
				return fmt.Errorf("report unreadable: %w", err)
			}
			rep.print(out)
			fmt.Fprintln(out, "report:", report)
			return nil
		}
		time.Sleep(5 * time.Second)
	}
	return fmt.Errorf("no report after %s - see the task's history (%s)", wait, probeTask)
}

func psQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }

// exposureOfProgram: who besides the system and the administrators could change this program (childpath.go rules).
func exposureOfProgram(exe string) (string, error) {
	e, err := childExposure(ChildSpec{Name: "probe", Cmd: exe})
	if err != nil || e == nil {
		return "", err
	}
	return e.Who, nil
}
