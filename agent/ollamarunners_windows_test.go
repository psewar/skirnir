//go:build windows

package main

import (
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

// runnersOf: the runner fact for a tree whose server process is root, with the real process queries.
func runnersOf(t *testing.T, tree *procTree, root int) OllamaRunners {
	t.Helper()
	pids, ok := tree.members()
	if !ok {
		t.Fatal("the job could not be listed")
	}
	return countRunners(pids, uint32(root), isConsoleHost, procMemory)
}

// The real job (ADR-0148): the test binary plays Ollama (parent) and its runner (grandchild), as in the supervisor tests.
// While the grandchild lives it is one runner with memory; once it has ended the count is 0.
func TestOllamaRunnersInARealJob(t *testing.T) {
	g := newGroup(t)
	cmd, tree, grandchild := startFamily(t, g, "eltern")
	defer tree.release()
	defer tree.kill()

	if r := runnersOf(t, tree, cmd.Process.Pid); r.Count != 1 {
		t.Fatalf("parent with one grandchild: %+v, want one runner", r)
	}
	// A freshly started process holds less than 1 MiB for a moment; the Go runtime's own soon holds more
	deadline := time.Now().Add(5 * time.Second)
	for r := runnersOf(t, tree, cmd.Process.Pid); r.PrivateMiB < 1; r = runnersOf(t, tree, cmd.Process.Pid) {
		if time.Now().After(deadline) {
			t.Fatalf("the runner's private working set never shows: %+v", r)
		}
		time.Sleep(50 * time.Millisecond)
	}
	h, err := windows.OpenProcess(windows.PROCESS_TERMINATE, false, uint32(grandchild))
	if err != nil {
		t.Fatal(err)
	}
	windows.TerminateProcess(h, 1)
	windows.CloseHandle(h)
	deadline = time.Now().Add(5 * time.Second)
	for runnersOf(t, tree, cmd.Process.Pid).Count != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("the runner ended, yet the job still lists it: %+v", runnersOf(t, tree, cmd.Process.Pid))
		}
		time.Sleep(50 * time.Millisecond)
	}
	// rejected: the server alone is never reported as a runner
	if r := runnersOf(t, tree, cmd.Process.Pid); r.Count != 0 || r.PrivateMiB != 0 {
		t.Fatalf("only the server left: %+v", r)
	}
}

// A console program without a console of its own (Ollama under the service) gets a console host from Windows, inside
// its job. That host is no runner: otherwise the count would never reach 0 after an unload.
func TestConsoleHostIsNoRunner(t *testing.T) {
	g := newGroup(t)
	attr := &syscall.SysProcAttr{HideWindow: true, CreationFlags: windows.CREATE_NEW_CONSOLE}
	cmd, tree, grandchild := startFamilyWith(t, g, "eltern", attr)
	defer tree.release()
	defer tree.kill()

	pids, ok := tree.members()
	if !ok {
		t.Fatal("the job could not be listed")
	}
	hosts := 0
	for _, pid := range pids {
		if isConsoleHost(pid) {
			hosts++
			if !strings.EqualFold(filepath.Base(processPath(pid)), "conhost.exe") {
				t.Fatalf("pid %d taken for the console host: %s", pid, processPath(pid))
			}
		}
	}
	if hosts == 0 {
		t.Fatalf("no console host in the job of a program with its own console (%v); the test no longer shows the case", pids)
	}
	r := countRunners(pids, uint32(cmd.Process.Pid), isConsoleHost, procMemory)
	if r.Count != 1 {
		t.Fatalf("parent, its console host and one grandchild (%d): %+v, want exactly the grandchild", grandchild, r)
	}
}

// Through the supervisor: two running Ollama children (one per GPU) are summed for the node; the STT child is no Ollama
// and does not count. Once one Ollama tree cannot be listed, the node gets no fact at all rather than half of it.
func TestOllamaRunnersSumOverOllamaChildren(t *testing.T) {
	g := newGroup(t)
	cmdA, treeA, _ := startFamily(t, g, "eltern")
	defer treeA.release()
	defer treeA.kill()
	cmdB, treeB, _ := startFamily(t, g, "eltern")
	defer treeB.release()
	defer treeB.kill()
	cmdC, treeC, _ := startFamily(t, g, "eltern")
	defer treeC.release()
	defer treeC.kill()
	s := &Supervisor{children: map[string]*childState{
		"ollama":   {spec: ChildSpec{Name: "ollama", Cmd: `C:\synthetic\Ollama\ollama.exe`}, Running: true, PID: cmdA.Process.Pid, tree: treeA},
		"ollama-2": {spec: ChildSpec{Name: "ollama-2", Cmd: `C:\synthetic\Ollama\ollama.exe`}, Running: true, PID: cmdB.Process.Pid, tree: treeB},
		"stt":      {spec: ChildSpec{Name: "stt", Cmd: `C:\synthetic\stt\stt.exe`}, Running: true, PID: cmdC.Process.Pid, tree: treeC},
	}}
	r, ok := s.ollamaRunners()
	if !ok || r.Count != 2 {
		t.Fatalf("two Ollama children with one runner each, plus an STT child: %+v ok=%v, want 2 runners", r, ok)
	}
	treeB.release() // its job handle is gone: that tree can no longer be listed
	if r, ok := s.ollamaRunners(); ok {
		t.Fatalf("one Ollama tree not listable, yet a fact for the node: %+v", r)
	}
}
