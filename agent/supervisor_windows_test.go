//go:build windows

package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

// Reproduces the 2026-09-23 incident: Ollama starts its model as a separate process
// (llama-server). The supervisor restarted Ollama but only terminated ollama.exe - the
// model process survived as an orphan with 23.5 GiB of GPU memory, keeping the node "busy" for hours.
//
// Here, the test file itself plays both roles: a "parent" (Ollama) starts a "grandchild"
// (llama-server), reports its PID, and then waits.

const helperEnv = "SKIRNIR_TEST_HELFER"

// TestHelperProcess is not a test, but the role switch for the child processes.
func TestHelperProcess(t *testing.T) {
	switch os.Getenv(helperEnv) {
	case "":
		t.Skip("nur als Kindprozess")
	case "enkel":
		time.Sleep(2 * time.Minute)
		os.Exit(0)
	case "sofort":
		bufio.NewReader(os.Stdin).ReadString('\n') // terminate only after attachment (no race condition)
		os.Exit(0)
	case "eltern", "eltern-kurz":
		// Start only after the test has attached the parent to the tree - otherwise the grandchild
		// runs outside the job, and the test would verify something other than the supervisor.
		bufio.NewReader(os.Stdin).ReadString('\n')
		grandchild := exec.Command(os.Args[0], "-test.run=^TestHelperProcess$")
		grandchild.Env = append(os.Environ(), helperEnv+"=enkel")
		if err := grandchild.Start(); err != nil {
			fmt.Println("FEHLER", err)
			os.Exit(3)
		}
		fmt.Println("ENKEL", grandchild.Process.Pid)
		if os.Getenv(helperEnv) == "eltern-kurz" {
			os.Exit(0) // "Crash": the parent exits, the grandchild remains
		}
		time.Sleep(2 * time.Minute)
		os.Exit(0)
	}
}

// checks if a process is still running (WaitForSingleObject with timeout 0).
func isAlive(pid int) bool {
	h, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false // no longer exists
	}
	defer windows.CloseHandle(h)
	ev, _ := windows.WaitForSingleObject(h, 0)
	return ev == uint32(windows.WAIT_TIMEOUT)
}

func waitDead(pid int, within time.Duration) bool {
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if !isAlive(pid) {
			return true
		}
		time.Sleep(50 * time.Millisecond)
	}
	return !isAlive(pid)
}

// startFamily starts a parent, attaches it to a tree, and returns the grandchild's PID.
func startFamily(t *testing.T, g *childGroup, role string) (*exec.Cmd, *procTree, int) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestHelperProcess$")
	cmd.Env = append(os.Environ(), helperEnv+"="+role)
	cmd.SysProcAttr = childProcAttr()
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	tree, err := g.track(cmd.Process.Pid)
	if err != nil {
		cmd.Process.Kill()
		t.Fatalf("track: %v", err)
	}
	fmt.Fprintln(stdin, "los")
	sc := bufio.NewScanner(stdout)
	for sc.Scan() {
		if f := strings.Fields(sc.Text()); len(f) == 2 && f[0] == "ENKEL" {
			pid, _ := strconv.Atoi(f[1])
			if !isAlive(pid) {
				t.Fatalf("Enkel %d lebt nicht einmal nach dem Start", pid)
			}
			return cmd, tree, pid
		}
	}
	t.Fatal("Elternteil hat keine Enkel-PID gemeldet")
	return nil, nil, 0
}

func newGroup(t *testing.T) *childGroup {
	t.Helper()
	g, err := newChildGroup()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { windows.CloseHandle(g.job) }) // KILL_ON_JOB_CLOSE cleans up leftovers
	return g
}

// Control case: this was the agent's behavior up to 0.5.1. Only the main process dies, the grandchild survives.
// If THIS test fails, the arrangement no longer checks what it should.
func TestEndingOnlyMainProcessLeavesGrandchild(t *testing.T) {
	g := newGroup(t)
	cmd, tree, grandchild := startFamily(t, g, "eltern")
	defer tree.release()
	defer tree.kill()

	killChild(cmd) // the old way
	cmd.Wait()
	if waitDead(grandchild, 2*time.Second) {
		t.Fatalf("Enkel %d ist mitgestorben - die Gegenprobe zeigt den alten Fehler nicht mehr", grandchild)
	}
}

// The 2026-09-23 bug: a restart must terminate the entire tree.
func TestRestartAlsoEndsGrandchild(t *testing.T) {
	g := newGroup(t)
	cmd, tree, grandchild := startFamily(t, g, "eltern")
	defer tree.release()

	stopChild(cmd, tree)
	cmd.Wait()
	if !waitDead(grandchild, 5*time.Second) {
		tree.kill()
		t.Fatalf("Enkel %d lebt nach dem Neustart weiter - genau der Waisen-Fehler", grandchild)
	}
}

// If the parent crashes on its own, reapTree cleans up the orphaned grandchild and reports it.
func TestCrashLeavesNoOrphan(t *testing.T) {
	g := newGroup(t)
	cmd, tree, grandchild := startFamily(t, g, "eltern-kurz")
	defer tree.release()
	cmd.Wait() // the parent is gone, the grandchild is still there

	if !isAlive(grandchild) {
		t.Fatal("Enkel ist schon ohne Aufraeumen tot - der Test beweist so nichts")
	}
	if n := tree.alive(); n != 1 {
		t.Fatalf("im Baum sollte genau der Enkel leben, gezaehlt: %d", n)
	}

	dir := t.TempDir()
	log, err := newLogger(LogCfg{Dir: dir}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() }) // Windows does not delete an open file
	s := &Supervisor{log: log}
	s.reapTree("ollama", tree)

	if !waitDead(grandchild, 5*time.Second) {
		t.Fatalf("verwaister Enkel %d lebt nach reapTree weiter", grandchild)
	}
	content := logContent(t, dir)
	if !strings.Contains(content, "ollama: 1 orphaned process(es) in the process tree ended") {
		t.Fatalf("Aufraeumen nicht im Log vermerkt:\n%s", content)
	}
}

// A child that exits cleanly and leaves nothing behind must not generate a warning.
func TestCleanEndWithoutWarning(t *testing.T) {
	g := newGroup(t)
	cmd := exec.Command(os.Args[0], "-test.run=^TestHelperProcess$")
	cmd.Env = append(os.Environ(), helperEnv+"=sofort")
	cmd.SysProcAttr = childProcAttr()
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	tree, err := g.track(cmd.Process.Pid)
	if err != nil {
		t.Fatal(err)
	}
	defer tree.release()
	fmt.Fprintln(stdin, "los")
	cmd.Wait()

	dir := t.TempDir()
	log, err := newLogger(LogCfg{Dir: dir}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() })
	(&Supervisor{log: log}).reapTree("stt", tree)
	if content := logContent(t, dir); strings.Contains(content, "orphaned") {
		t.Fatalf("Warnung ohne Waise:\n%s", content)
	}
}

func logContent(t *testing.T, dir string) string {
	t.Helper()
	var whole strings.Builder
	filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			b, _ := os.ReadFile(p)
			whole.Write(b)
		}
		return nil
	})
	return whole.String()
}
