package main

import (
	"strings"
	"testing"
)

// ADR-0087: the probe's pure parts - options, which children it starts, the probe port, the CUDA reading.

func TestParseProbeArgs(t *testing.T) {
	o, err := parseProbeArgs([]string{"probe-identity", "--config", "x.yaml"})
	if err != nil || len(o.Tokens) != 3 || o.Grace.Seconds() != 150 || len(o.Children) != 0 {
		t.Fatalf("defaults: %+v %v", o, err)
	}
	o, err = parseProbeArgs([]string{"--config=x.yaml", "probe-identity", "--child", "stt, ollama", "--token", "restricted",
		"--with-upstream", "measure strata once", "--grace-s", "60", "--out", "r.json"})
	if err != nil || strings.Join(o.Children, ",") != "stt,ollama" || strings.Join(o.Tokens, ",") != "restricted" ||
		o.WithUpstream == "" || o.Grace.Seconds() != 60 || o.Out != "r.json" {
		t.Fatalf("options: %+v %v", o, err)
	}
	for _, bad := range [][]string{{"--token", "admin"}, {"--with-upstream", " "}, {"--grace-s", "5"}, {"--child"}, {"--what"}} {
		if _, err := parseProbeArgs(bad); err == nil {
			t.Errorf("%v must be refused", bad)
		}
	}
}

func TestProbeChildren(t *testing.T) {
	off := false
	cfg := &Config{}
	cfg.Tunnel.UpstreamChild = "strata"
	cfg.Children = []ChildSpec{
		{Name: "ollama", Cmd: "o", HealthTCPPort: 11434},
		{Name: "stt", Cmd: "s", HealthTCPPort: 10300},
		{Name: "strata", Cmd: "x", HealthTCPPort: 8080},
		{Name: "old", Cmd: "y", HealthTCPPort: 1, Enabled: &off},
		{Name: "noport", Cmd: "z"},
	}
	run, skipped := probeChildren(cfg, ProbeOptions{})
	if len(run) != 2 || run[0].Name != "ollama" || run[1].Name != "stt" {
		t.Fatalf("default: %v", run)
	}
	for _, name := range []string{"strata", "old", "noport"} {
		if skipped[name] == "" {
			t.Errorf("%s must be listed as skipped with a reason", name)
		}
	}
	run, _ = probeChildren(cfg, ProbeOptions{Children: []string{"strata"}, WithUpstream: "once, low RAM"})
	if len(run) != 1 || run[0].Name != "strata" {
		t.Fatalf("upstream with a reason: %v", run)
	}
	_, skipped = probeChildren(cfg, ProbeOptions{Children: []string{"missing"}})
	if skipped["missing"] == "" {
		t.Fatal("an unknown child is reported")
	}
}

func TestProbeSpecPort(t *testing.T) {
	sp := ChildSpec{Name: "stt", HealthTCPPort: 10300,
		Args: []string{"app.py", "--uri", "tcp://0.0.0.0:10300", "--other", "110300", "103001"},
		Env:  []string{"OLLAMA_HOST=127.0.0.1:10300", "KEEP=10300x"}}
	p := probeSpec(sp, 41234)
	if p.Args[2] != "tcp://0.0.0.0:41234" || p.Args[4] != "110300" || p.Args[5] != "103001" {
		t.Fatalf("args: %v", p.Args)
	}
	if p.Env[0] != "OLLAMA_HOST=127.0.0.1:41234" || p.Env[1] != "KEEP=41234x" || p.HealthTCPPort != 41234 {
		t.Fatalf("env: %v", p.Env)
	}
	if sp.Args[2] != "tcp://0.0.0.0:10300" {
		t.Fatal("the original spec must stay unchanged")
	}
}

func TestCUDAInOutput(t *testing.T) {
	cases := map[string][]string{
		"seen":     {`time=... level=INFO msg="inference compute" id=GPU-1 library=cuda compute=12.0`},
		"not seen": {"listening on 127.0.0.1:41234", "no gpu here"},
	}
	for want, lines := range cases {
		if got := cudaInOutput(lines); got != want {
			t.Errorf("%v: %s, want %s", lines, got, want)
		}
	}
	if got := cudaInOutput([]string{"CUDA initialization failed: access denied"}); !strings.HasPrefix(got, "error") {
		t.Errorf("a CUDA failure must be reported as error, got %s", got)
	}
}
