package main

import (
	"bytes"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// ADR-0085: a child whose program someone besides the system and the administrators could change is not started.

func systemProgram(t *testing.T) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe")
	}
	p, err := exec.LookPath("sh")
	if err != nil {
		t.Skip("no sh")
	}
	return p
}

// userProgram: an executable in a folder this test (a user, or root with an open folder) controls.
func userProgram(t *testing.T) string {
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o777); err != nil { // Unix: open for everyone even when the test runs as root
		t.Fatal(err)
	}
	name := "child"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte("x"), 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestChildExposureSystemProgram(t *testing.T) {
	prog := systemProgram(t)
	if runtime.GOOS != "windows" && os.Geteuid() != 0 {
		if fi, err := os.Stat(prog); err != nil || fi.Sys() == nil {
			t.Skip("no owner information")
		}
	}
	e, err := childExposure(ChildSpec{Name: "sys", Cmd: prog})
	if err != nil {
		t.Fatalf("check failed: %v", err)
	}
	if e != nil {
		t.Fatalf("a system program must not be reported: %s", e)
	}
}

func TestChildExposureUserProgram(t *testing.T) {
	prog := userProgram(t)
	e, err := childExposure(ChildSpec{Name: "ollama", Cmd: prog})
	if err != nil {
		t.Fatalf("check failed: %v", err)
	}
	if e == nil || e.Child != "ollama" || e.Who == "" {
		t.Fatalf("a program in a user-controlled folder must be reported: %+v", e)
	}
	// The check reports resolved paths: a temp folder given in its 8.3 short form (a "~1" name, as on CI runners) comes
	// back in its long form, so the expectation is resolved the same way; Windows compares paths without case.
	want, err := filepath.EvalSymlinks(prog)
	if err != nil {
		t.Fatal(err)
	}
	same := func(a, b string) bool { return a == b || (runtime.GOOS == "windows" && strings.EqualFold(a, b)) }
	if !same(e.Path, want) && !same(e.Path, filepath.Dir(want)) {
		t.Fatalf("the report must name the program or its folder (%s), got %s", want, e.Path)
	}
}

func TestChildExposureUnresolvable(t *testing.T) {
	e, err := childExposure(ChildSpec{Name: "gone", Cmd: filepath.Join(t.TempDir(), "missing-program")})
	if e != nil || err != nil {
		t.Fatalf("a command that cannot be found is left to the start: %+v %v", e, err)
	}
}

func TestReportChildExposures(t *testing.T) {
	prog := userProgram(t)
	var out bytes.Buffer
	err := reportChildExposures([]ChildSpec{{Name: "ollama", Cmd: prog}}, &out)
	if err == nil || !strings.HasPrefix(out.String(), "refused: child ollama: ") || !strings.Contains(out.String(), "user_writable_reason") {
		t.Fatalf("an unaccepted child must be refused with the next step: %v\n%s", err, out.String())
	}
	out.Reset()
	err = reportChildExposures([]ChildSpec{{Name: "ollama", Cmd: prog, UserWritableReason: "test install"}}, &out)
	if err != nil || !strings.HasPrefix(out.String(), "accepted: child ollama: ") || !strings.Contains(out.String(), "test install") {
		t.Fatalf("an accepted child is reported with its reason, without error: %v\n%s", err, out.String())
	}
	off := false
	out.Reset()
	if err := reportChildExposures([]ChildSpec{{Name: "ollama", Cmd: prog, Enabled: &off}}, &out); err != nil || out.Len() != 0 {
		t.Fatalf("a disabled child is not checked: %v %q", err, out.String())
	}
}

func TestSupervisorRefusesExposedChild(t *testing.T) {
	old := exposureRecheck
	exposureRecheck = 20 * time.Millisecond
	defer func() { exposureRecheck = old }()
	sp := ChildSpec{Name: "ollama", Cmd: filepath.Join(t.TempDir(), "never-started"), StartupGraceS: 1}
	logc := LogCfg{Dir: t.TempDir()}
	log, err := newLogger(logc, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() }) // Windows does not delete open files
	s, err := newSupervisor([]ChildSpec{sp}, logc, log, t.TempDir())
	s.launch = testLaunch
	if err != nil {
		t.Fatal(err)
	}
	exposed := &ChildExposure{Child: "ollama", Path: sp.Cmd, Who: "someone"}
	s.exposure = func(ChildSpec) (*ChildExposure, error) { return exposed, nil }
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	s.loop(ctx, sp)
	st := s.Status()["ollama"]
	if st.Refused == "" || st.Restarts != 0 || st.Exposure == nil {
		t.Fatalf("an exposed child must be refused without a start attempt: %+v", st)
	}

	// Accepted with a reason: the supervisor tries to start it (the program does not exist, so the attempt fails).
	accepted := *exposed
	accepted.Reason = "test"
	s.exposure = func(ChildSpec) (*ChildExposure, error) { return &accepted, nil }
	ctx2, cancel2 := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel2()
	s.loop(ctx2, sp)
	st = s.Status()["ollama"]
	if st.Refused != "" || st.Restarts == 0 {
		t.Fatalf("an accepted child must be started: %+v", st)
	}
}
