package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// testLaunch starts a child with the test's own rights (the CI runner is not LocalSystem and cannot log on LOCAL SERVICE).
func testLaunch(ChildSpec) (*childLaunch, error) {
	return &childLaunch{runAs: "test", attr: childProcAttr(), env: os.Environ(), release: func() {}}, nil
}

func TestValidateRunAs(t *testing.T) {
	cases := []struct {
		sp   ChildSpec
		goos string
		want string // "" = valid; else a part of the error
	}{
		{ChildSpec{Name: "a"}, "windows", ""},
		{ChildSpec{Name: "a", RunAs: "localservice"}, "windows", ""},
		{ChildSpec{Name: "a", RunAs: "system", RunAsReason: "needs the desktop session"}, "windows", ""},
		{ChildSpec{Name: "a", RunAs: "system"}, "windows", "needs run_as_reason"},
		{ChildSpec{Name: "a", RunAs: "system", RunAsReason: "  "}, "windows", "needs run_as_reason"},
		{ChildSpec{Name: "a", RunAs: "restricted"}, "windows", "is not localservice or system"},
		{ChildSpec{Name: "a", RunAsReason: "x"}, "windows", "only for run_as: system"},
		{ChildSpec{Name: "a", ReadPaths: []string{`C:\models`}, WritePaths: []string{`\\nas\share\x`}}, "windows", ""},
		{ChildSpec{Name: "a", ReadPaths: []string{`models`}}, "windows", "not an absolute path"},
		{ChildSpec{Name: "a"}, "linux", ""},
		{ChildSpec{Name: "a", RunAs: "localservice"}, "linux", "only on Windows"},
		{ChildSpec{Name: "a", ReadPaths: []string{"/srv/models"}}, "linux", "only on Windows"},
	}
	for _, c := range cases {
		err := validateRunAs(c.sp, c.goos)
		switch {
		case c.want == "" && err != nil:
			t.Errorf("%+v on %s: unexpected error %v", c.sp, c.goos, err)
		case c.want != "" && (err == nil || !strings.Contains(err.Error(), c.want)):
			t.Errorf("%+v on %s: want error containing %q, got %v", c.sp, c.goos, c.want, err)
		}
	}
}

func TestChildRunAs(t *testing.T) {
	if got := childRunAs(ChildSpec{}, "windows"); got != runAsLocalService {
		t.Fatalf("Windows default must be localservice, got %s", got)
	}
	if got := childRunAs(ChildSpec{RunAs: "system", RunAsReason: "x"}, "windows"); got != runAsSystem {
		t.Fatalf("got %s", got)
	}
	if got := childRunAs(ChildSpec{}, "linux"); got != runAsAgent {
		t.Fatalf("other platforms run children as the agent's account, got %s", got)
	}
}

// A child whose identity cannot be prepared is refused without a start attempt and without falling back to more rights.
func TestSupervisorRefusesChildWithoutIdentity(t *testing.T) {
	old := exposureRecheck
	exposureRecheck = 20 * time.Millisecond
	defer func() { exposureRecheck = old }()
	sp := ChildSpec{Name: "stt", Cmd: filepath.Join(t.TempDir(), "never-started"), StartupGraceS: 1}
	logc := LogCfg{Dir: t.TempDir()}
	log, err := newLogger(logc, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() })
	s, err := newSupervisor([]ChildSpec{sp}, logc, log, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	s.exposure = func(ChildSpec) (*ChildExposure, error) { return nil, nil }
	denied := []ChildAccess{{Path: `C:\models`, Need: needRead, From: "arg", State: "denied: grant failed: test"}}
	s.launch = func(ChildSpec) (*childLaunch, error) {
		return &childLaunch{runAs: runAsLocalService, access: denied, release: func() {}}, errors.New("read access to C:\\models: denied")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	s.loop(ctx, sp)
	st := s.Status()["stt"]
	if st.Restarts != 0 || !strings.Contains(st.Refused, "cannot start as localservice") || st.RunAs != runAsLocalService ||
		len(st.Access) != 1 || !st.Access[0].refuses() {
		t.Fatalf("refused child expected, with its identity and access as facts: %+v", st)
	}
}

func TestReportChildIdentities(t *testing.T) {
	var out bytes.Buffer
	off := false
	specs := []ChildSpec{{Name: "stt", Cmd: "x"}, {Name: "off", Cmd: "y", Enabled: &off}}
	if err := reportChildIdentities(specs, t.TempDir(), &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "identity: child stt runs as ") || strings.Contains(out.String(), "child off") {
		t.Fatalf("one line per enabled child expected:\n%s", out.String())
	}
}
