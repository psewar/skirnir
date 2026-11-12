//go:build windows

package main

import (
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// ADR-0087: the restricted token has no administrator rights and medium integrity, and a child started under it is
// measured end to end: started, healthy on its probe port, CUDA line seen, process tree ended.

func tokenIntegrity(t *testing.T, tok windows.Token) string {
	var n uint32
	_ = windows.GetTokenInformation(tok, windows.TokenIntegrityLevel, nil, 0, &n)
	buf := make([]byte, n)
	if err := windows.GetTokenInformation(tok, windows.TokenIntegrityLevel, &buf[0], n, &n); err != nil {
		t.Fatal(err)
	}
	return (*windows.Tokenmandatorylabel)(unsafe.Pointer(&buf[0])).Label.Sid.String()
}

func TestProbeTokenRestricted(t *testing.T) {
	tok, err := probeToken("restricted")
	if err != nil {
		t.Fatalf("restricted token: %v", err)
	}
	defer tok.Close()
	admins, _ := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	var imp windows.Token
	if err := windows.DuplicateTokenEx(tok, windows.TOKEN_QUERY, nil, windows.SecurityIdentification, windows.TokenImpersonation, &imp); err != nil {
		t.Fatal(err)
	}
	defer imp.Close()
	if member, err := imp.IsMember(admins); err != nil || member {
		t.Fatalf("the restricted token must not act as an administrator (member=%v, %v)", member, err)
	}
	if got := tokenIntegrity(t, tok); got != "S-1-16-8192" {
		t.Fatalf("integrity must be medium, got %s", got)
	}
}

const probeHelperEnv = "SKIRNIR_PROBE_HELFER"

// TestProbeHelperProcess is not a test but the stand-in child: it listens on the port in its last argument and prints a
// CUDA line, as Ollama does at start.
func TestProbeHelperProcess(t *testing.T) {
	if os.Getenv(probeHelperEnv) != "listen" {
		t.Skip("only as a child process")
	}
	port := os.Args[len(os.Args)-1]
	l, err := net.Listen("tcp", "127.0.0.1:"+port)
	if err != nil {
		os.Exit(3)
	}
	defer l.Close()
	fmt.Println(`msg="inference compute" id=GPU-0 library=cuda`)
	time.Sleep(2 * time.Minute)
	os.Exit(0)
}

func TestProbeChildRestricted(t *testing.T) {
	tok, err := probeToken("restricted")
	if err != nil {
		t.Fatal(err)
	}
	defer tok.Close()
	// The restricted token denies the user's own SID, so the test binary in the user's temp folder is not executable
	// for it (that is the point). The stand-in runs from a copy that everyone may read and execute.
	dir := t.TempDir()
	exe := filepath.Join(dir, "standin.exe")
	src, err := os.ReadFile(os.Args[0])
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(exe, src, 0o755); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("icacls.exe", dir, "/grant", "*S-1-1-0:(OI)(CI)RX").CombinedOutput(); err != nil {
		t.Fatalf("icacls: %v %s", err, out)
	}
	sp := ChildSpec{Name: "standin", Cmd: exe, Args: []string{"-test.run=TestProbeHelperProcess", "--", "39871"},
		Env: []string{probeHelperEnv + "=listen"}, Cwd: dir, HealthTCPPort: 39871, StartupGraceS: 30}
	old := probeSettle
	probeSettle = 3 * time.Second // the stand-in uses no GPU memory; no need to wait the full window
	defer func() { probeSettle = old }()
	r := probeChild(sp, "restricted", tok, 60*time.Second)
	if !r.Started || !r.Healthy {
		t.Fatalf("stand-in child under the restricted token must start and listen on its probe port: %+v", r)
	}
	if r.Port == 39871 {
		t.Fatal("the probe must use a port of its own, not the child's configured one")
	}
	if r.CUDA != "seen" || !r.TreeEnded {
		t.Fatalf("CUDA line and process tree: %+v", r)
	}
}
