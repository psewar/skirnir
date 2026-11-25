package main

import (
	"encoding/json"
	"testing"
)

// The classification (ADR-0148) on every platform, with stand-ins for the process queries.
func TestCountRunners(t *testing.T) {
	const server, host, runner, other, gone = 100, 101, 102, 103, 104
	consoleHost := func(pid uint32) bool { return pid == host }
	memory := func(pid uint32) (procMem, bool) {
		switch pid {
		case server:
			return procMem{Private: 900 << 20}, true
		case host:
			return procMem{Private: 8 << 20}, true
		case runner:
			return procMem{Private: 3<<30 + 512<<20}, true
		case other:
			return procMem{Private: 700 << 20}, true
		}
		return procMem{}, false // ended between listing and reading
	}
	cases := []struct {
		name    string
		pids    []uint32
		count   int
		private int
	}{
		{"server and console host only: released", []uint32{server, host}, 0, 0},
		{"empty job", []uint32{}, 0, 0},
		{"one runner", []uint32{server, host, runner}, 1, 3584},
		{"two runners are summed", []uint32{server, runner, other}, 2, 4284},
		// rejected: a process whose memory cannot be read still counts, so the fact never says "released" too early
		{"unreadable process still counts", []uint32{server, host, gone}, 1, 0},
	}
	for _, c := range cases {
		r := countRunners(c.pids, server, consoleHost, memory)
		if r.Count != c.count || r.PrivateMiB != c.private {
			t.Errorf("%s: got %+v, want count %d private %d MiB", c.name, r, c.count, c.private)
		}
	}
	// rejected: the server itself is never a runner, even when the console host check does not know it
	if r := countRunners([]uint32{server}, server, func(uint32) bool { return false }, memory); r.Count != 0 {
		t.Errorf("the server counted as its own runner: %+v", r)
	}
}

// The wire form is the schema's: {"count": n, "private_mib": n}, both always present (0 is the "released" fact).
func TestOllamaRunnersWireForm(t *testing.T) {
	b, err := json.Marshal(OllamaRunners{})
	if err != nil || string(b) != `{"count":0,"private_mib":0}` {
		t.Fatalf("got %s (%v)", b, err)
	}
}

// Without an Ollama child (or without a supervisor) the agent cannot tell and sends nothing.
func TestOllamaRunnersNeedAnOllamaChild(t *testing.T) {
	var none *Supervisor
	if _, ok := none.ollamaRunners(); ok {
		t.Fatal("no supervisor, yet a runner fact")
	}
	s := &Supervisor{children: map[string]*childState{
		"stt":    {spec: ChildSpec{Name: "stt", Cmd: `C:\synthetic\stt.exe`}, Running: true},
		"ollama": {spec: ChildSpec{Name: "ollama", Cmd: `C:\synthetic\ollama.exe`}, Running: false},
	}}
	if _, ok := s.ollamaRunners(); ok {
		t.Fatal("Ollama not running, yet a runner fact")
	}
	s.children["ollama"].Running = true // running, but its tree is not tracked: still nothing to list
	if _, ok := s.ollamaRunners(); ok {
		t.Fatal("Ollama without a tracked process tree, yet a runner fact")
	}
}
