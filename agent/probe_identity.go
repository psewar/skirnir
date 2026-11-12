package main

import (
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// probe-identity (ADR-0087): measures, before any code changes how children are started, what each child and the agent's
// privileged functions need when they run under a token without system rights. Nothing in the running service changes:
//
//   - every enabled child (except the upstream child, which loads a large model - only with --with-upstream "<reason>")
//     starts once per token on a probe port of its own (its health port number replaced in args and env), in a job object;
//     the probe records whether it starts and becomes healthy, whether its output shows CUDA, how much GPU memory the
//     per-process counter attributes to it, and whether ending the job ends the whole process tree; then it stops it.
//   - the agent's own privileged functions run in a copy of this program under each token: NVML power limit set to its
//     current value (a no-op that shows the permission), GPU-Z, paths of other users' processes, other users' hives.
//
// Tokens: system (the probe's own, the baseline), restricted (the same token with administrators and the user SID
// deny-only, privileges removed, integrity medium), localservice (the built-in LOCAL SERVICE account). The probe must run
// as LocalSystem from a folder only administrators can change (the children it starts get these tokens); started as an
// administrator it says so and how. Facts, not judgements: the report is JSON, one line per result, last line STATE.

var probeTokenKinds = []string{"system", "restricted", "localservice"}

// ProbeOptions: command line of probe-identity.
type ProbeOptions struct {
	Children     []string      // empty = every enabled child except the upstream child
	Tokens       []string      // empty = all of probeTokenKinds
	WithUpstream string        // reason to include the upstream child
	Out          string        // JSON report file; empty = stdout only
	Grace        time.Duration // longest wait for a child's health port
	InnerRights  bool          // internal: run the rights checks in this process and print JSON
}

func parseProbeArgs(args []string) (ProbeOptions, error) {
	o := ProbeOptions{Grace: 150 * time.Second}
	for i := 0; i < len(args); i++ {
		a := args[i]
		val := func() (string, error) {
			if i+1 >= len(args) {
				return "", fmt.Errorf("%s needs a value", a)
			}
			i++
			return args[i], nil
		}
		switch a {
		case "--child", "--token", "--with-upstream", "--out", "--grace-s":
			v, err := val()
			if err != nil {
				return o, err
			}
			switch a {
			case "--child":
				o.Children = append(o.Children, splitList(v)...)
			case "--token":
				for _, t := range splitList(v) {
					if !containsStr(probeTokenKinds, t) {
						return o, fmt.Errorf("unknown token %q (system, restricted, localservice)", t)
					}
					o.Tokens = append(o.Tokens, t)
				}
			case "--with-upstream":
				if strings.TrimSpace(v) == "" {
					return o, fmt.Errorf("--with-upstream needs a reason")
				}
				o.WithUpstream = v
			case "--out":
				o.Out = v
			case "--grace-s":
				n, err := strconv.Atoi(v)
				if err != nil || n < 10 || n > 900 {
					return o, fmt.Errorf("--grace-s must be 10..900")
				}
				o.Grace = time.Duration(n) * time.Second
			}
		case "--inner-rights":
			o.InnerRights = true
		case "--config":
			i++ // read by main
		case "probe-identity": // the verb itself (main passes all arguments)
		default:
			if strings.HasPrefix(a, "--config=") {
				continue
			}
			return o, fmt.Errorf("unknown option %s", a)
		}
	}
	if len(o.Tokens) == 0 {
		o.Tokens = append([]string(nil), probeTokenKinds...)
	}
	return o, nil
}

func splitList(v string) []string {
	var out []string
	for _, s := range strings.Split(v, ",") {
		if s = strings.TrimSpace(s); s != "" {
			out = append(out, s)
		}
	}
	return out
}

func containsStr(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// probeChildren: which children the probe starts, and the ones it leaves out with the reason.
func probeChildren(cfg *Config, o ProbeOptions) (run []ChildSpec, skipped map[string]string) {
	skipped = map[string]string{}
	upstream := cfg.Tunnel.UpstreamChild
	for _, sp := range cfg.Children {
		switch {
		case len(o.Children) > 0 && !containsStr(o.Children, sp.Name):
			continue
		case !sp.on():
			skipped[sp.Name] = "disabled"
		case sp.Name == upstream && o.WithUpstream == "":
			skipped[sp.Name] = "upstream child (loads a large model) - include with --with-upstream \"<reason>\""
		case sp.HealthTCPPort == 0:
			skipped[sp.Name] = "no health_tcp_port: the probe cannot give it a port of its own"
		default:
			run = append(run, sp)
		}
	}
	for _, name := range o.Children {
		found := false
		for _, sp := range cfg.Children {
			found = found || sp.Name == name
		}
		if !found {
			skipped[name] = "no such child"
		}
	}
	return run, skipped
}

// probeSpec: the child with its health port number replaced by port wherever it stands as a whole number in args and
// env, so the probe copy does not collide with the running child.
func probeSpec(sp ChildSpec, port int) ChildSpec {
	re := regexp.MustCompile(`(^|[^0-9])` + strconv.Itoa(sp.HealthTCPPort) + `([^0-9]|$)`)
	sub := func(s string) string { return re.ReplaceAllString(s, "${1}"+strconv.Itoa(port)+"${2}") }
	out := sp
	out.Args = make([]string, len(sp.Args))
	for i, a := range sp.Args {
		out.Args[i] = sub(a)
	}
	out.Env = make([]string, len(sp.Env))
	for i, e := range sp.Env {
		out.Env[i] = sub(e)
	}
	out.HealthTCPPort = port
	return out
}

var (
	// "cuda" as a word of its own: not part of an identifier such as CUDA_VISIBLE_DEVICES, which Ollama lists at every start
	// whether a GPU is usable or not (first measurement on a node, 2026-11-12: a false "seen").
	cudaWord    = regexp.MustCompile(`(?i)(^|[^a-z0-9_])cuda([^a-z0-9_]|$)`)
	cudaSuccess = regexp.MustCompile(`(?i)library=cuda|\b(bereit|ready|loaded|initiali[sz]ed)\b`)
	cudaFailure = regexp.MustCompile(`(?i)\b(fail\w*|error|denied|not available|unavailable|no cuda)\b`)
)

// cudaInOutput: whether the child's output proves it uses CUDA - Ollama's "inference compute ... library=cuda", or a line
// that names CUDA as loaded or ready (the STT service: "CUDA-Whisper bereit"). A line that names CUDA with a failure
// is reported as an error; announcements ("CUDA wird geladen") prove nothing.
func cudaInOutput(lines []string) string {
	failure := ""
	for _, l := range lines {
		if !cudaWord.MatchString(l) {
			continue
		}
		switch {
		case cudaFailure.MatchString(l):
			failure = "error: " + strings.TrimSpace(l)
		case cudaSuccess.MatchString(l):
			return "seen"
		}
	}
	if failure != "" {
		return failure
	}
	return "not seen"
}

// ProbeChildResult: one child under one token.
type ProbeChildResult struct {
	Child     string   `json:"child"`
	Token     string   `json:"token"`
	Port      int      `json:"port"`
	Started   bool     `json:"started"`
	StartErr  string   `json:"start_error,omitempty"`
	Healthy   bool     `json:"healthy"`
	HealthyS  float64  `json:"healthy_after_s,omitempty"`
	ExitCode  *int     `json:"exit_code,omitempty"` // when it ended before it was healthy
	CUDA      string   `json:"cuda"`
	VRAMMiB   uint64   `json:"vram_mib"`
	TreeEnded bool     `json:"tree_ended"`
	LogTail   []string `json:"log_tail,omitempty"`
}

// ProbeRights: the agent's own privileged functions under one token.
type ProbeRights struct {
	Token     string `json:"token"`
	NVMLSet   string `json:"nvml_power_limit"`
	GPUZ      string `json:"gpuz"`
	ProcPaths string `json:"process_paths"`
	UserHives string `json:"user_hives"`
	Err       string `json:"error,omitempty"`
}

// ProbeReport is what probe-identity writes.
type ProbeReport struct {
	Node         string             `json:"node"`
	AgentVersion string             `json:"agent_version"`
	RunningAs    string             `json:"running_as"`
	At           time.Time          `json:"at"`
	Tokens       map[string]string  `json:"tokens"` // kind -> "ok" or why it could not be created
	Children     []ProbeChildResult `json:"children"`
	Skipped      map[string]string  `json:"skipped,omitempty"`
	Rights       []ProbeRights      `json:"rights"`
}

func (r ProbeReport) print(w io.Writer) {
	fmt.Fprintf(w, "probe-identity %s on %s as %s\n", r.AgentVersion, r.Node, r.RunningAs)
	for _, k := range probeTokenKinds {
		if v, ok := r.Tokens[k]; ok {
			fmt.Fprintf(w, "token %-12s %s\n", k, v)
		}
	}
	for _, c := range r.Children {
		state := "not started: " + c.StartErr
		if c.Started {
			state = fmt.Sprintf("started, healthy=%v", c.Healthy)
			if c.Healthy {
				state += fmt.Sprintf(" after %.0f s", c.HealthyS)
			} else if c.ExitCode != nil {
				state += fmt.Sprintf(" (ended, exit %d)", *c.ExitCode)
			}
		}
		fmt.Fprintf(w, "child %-8s %-12s %s; cuda %s; vram %d MiB; tree ended %v\n", c.Child, c.Token, state, c.CUDA, c.VRAMMiB, c.TreeEnded)
	}
	for name, why := range r.Skipped {
		fmt.Fprintf(w, "child %-8s skipped: %s\n", name, why)
	}
	for _, x := range r.Rights {
		if x.Err != "" {
			fmt.Fprintf(w, "rights %-12s %s\n", x.Token, x.Err)
			continue
		}
		fmt.Fprintf(w, "rights %-12s nvml %s; gpu-z %s; process paths %s; user hives %s\n", x.Token, x.NVMLSet, x.GPUZ, x.ProcPaths, x.UserHives)
	}
}

func (r ProbeReport) json() []byte {
	b, _ := json.MarshalIndent(r, "", " ")
	return b
}
