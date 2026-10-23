package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"sync"
	"time"
)

// Supervisor starts and monitors child processes (Ollama, Wyoming-STT). Windows: all children attach to a single
// Job object with KILL_ON_JOB_CLOSE - if the service dies, they die with it - and each child additionally to its own
// sub-job (procTree), so a restart also terminates its grandchildren. Linux: own process group.
type Supervisor struct {
	specs []ChildSpec
	logc  LogCfg
	log   *Logger
	group *childGroup

	mu       sync.Mutex
	children map[string]*childState

	truncations *Truncations // stille Kuerzungen im Ollama-Log (truncation.go)
}

type childState struct {
	spec      ChildSpec
	Running   bool      `json:"running"`
	Healthy   bool      `json:"healthy"`
	PID       int       `json:"pid,omitempty"`
	Restarts  int       `json:"restarts"`
	Since     time.Time `json:"since"`
	LastExit  string    `json:"last_exit,omitempty"`
	NextStart time.Time `json:"next_start,omitempty"`
	kill      func()
	held      bool      // Hold(): paused, no restart until Release() (Ollama update, ollamaupdate.go)
	tree      *procTree // Job object of the running child (0.13.0: VRAM per child)
}

var restartBackoff = []time.Duration{time.Second, 5 * time.Second, 30 * time.Second, 60 * time.Second}

func newSupervisor(specs []ChildSpec, logc LogCfg, log *Logger) (*Supervisor, error) {
	g, err := newChildGroup()
	if err != nil {
		return nil, err
	}
	s := &Supervisor{specs: specs, logc: logc, log: log, group: g, children: map[string]*childState{},
		truncations: newTruncations()}
	for _, sp := range specs {
		s.children[sp.Name] = &childState{spec: sp}
	}
	return s, nil
}

func (s *Supervisor) Run(ctx context.Context) {
	var wg sync.WaitGroup
	for _, sp := range s.specs {
		if !sp.on() {
			s.log.Infof("supervisor: child %s disabled", sp.Name)
			continue
		}
		wg.Add(1)
		go func(sp ChildSpec) { defer wg.Done(); s.loop(ctx, sp) }(sp)
	}
	wg.Wait()
}

func (s *Supervisor) loop(ctx context.Context, sp ChildSpec) {
	st := s.children[sp.Name]
	out := newChildWriter(s.logc, sp.Name)
	name := sp.Name
	out.onLine = func(z string) {
		if s.truncations.inspect(name, z) {
			s.log.Warnf("supervisor: %s truncated a prompt: %s", name, z)
		}
	}
	attempt := 0
	for {
		if !s.waitWhileHeld(ctx, sp.Name) { // held before it ever ran (Park: standby fallback, upstream_hold.go)
			return
		}
		start := time.Now()
		exitMsg, err := s.runOnce(ctx, sp, st, out)
		if ctx.Err() != nil {
			return
		}
		ran := time.Since(start)
		if ran > 5*time.Minute {
			attempt = 0 // ran stable for a long time: reset backoff
		}
		delay := restartBackoff[min(attempt, len(restartBackoff)-1)]
		attempt++
		s.mu.Lock()
		held := st.held
		st.Running, st.Healthy, st.PID = false, false, 0
		st.LastExit = fmt.Sprintf("%s after %s", exitMsg, ran.Round(time.Second))
		st.Restarts++
		st.NextStart = time.Now().Add(delay)
		if held {
			st.NextStart = time.Time{}
		}
		s.mu.Unlock()
		if held { // Ollama update or upstream hold: paused until Release() - then restart immediately, without backoff
			s.log.Infof("supervisor: %s stopped (held), waiting for release", sp.Name)
			attempt = 0
			continue // waitWhileHeld at the top of the loop
		}
		if err != nil {
			s.log.Warnf("supervisor: %s: %s, restart in %s", sp.Name, exitMsg, delay)
		} else {
			s.log.Warnf("supervisor: %s exited (%s), restart in %s", sp.Name, exitMsg, delay)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
	}
}

// runOnce starts the child, attaches it to the group, checks health, and waits for exit.
func (s *Supervisor) runOnce(ctx context.Context, sp ChildSpec, st *childState, out *childWriter) (string, error) {
	cmd := exec.Command(sp.Cmd, sp.Args...)
	cmd.Dir = sp.Cwd
	cmd.Env = append(os.Environ(), sp.Env...)
	cmd.Stdout, cmd.Stderr = out, out
	cmd.SysProcAttr = childProcAttr()
	if err := cmd.Start(); err != nil {
		return fmt.Sprintf("start failed: %v", err), err
	}
	tree, err := s.group.track(cmd.Process.Pid)
	if err != nil {
		s.log.Warnf("supervisor: %s: process group: %v", sp.Name, err)
	}
	// Release handle at the end; on Windows this (KILL_ON_JOB_CLOSE) also terminates any remaining processes.
	defer tree.release()
	defer func() { s.mu.Lock(); st.tree = nil; s.mu.Unlock() }()
	s.mu.Lock()
	st.Running, st.Healthy, st.PID, st.Since, st.NextStart = true, sp.HealthTCPPort == 0, cmd.Process.Pid, time.Now(), time.Time{}
	st.kill = func() { stopChild(cmd, tree) }
	st.tree = tree
	s.mu.Unlock()
	s.log.Infof("supervisor: %s started (PID %d): %s %v", sp.Name, cmd.Process.Pid, sp.Cmd, sp.Args)

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	// Health check: after the grace period, the port must be open, otherwise 3 failed attempts -> restart
	var hc <-chan time.Time
	if sp.HealthTCPPort > 0 {
		t := time.NewTicker(15 * time.Second)
		defer t.Stop()
		hc = t.C
	}
	grace := time.Now().Add(time.Duration(sp.StartupGraceS * float64(time.Second)))
	misses := 0
	for {
		select {
		case <-ctx.Done():
			stopChild(cmd, tree)
			<-done
			return "Dienst-Stopp", nil
		case err := <-done:
			s.reapTree(sp.Name, tree)
			if err != nil {
				return fmt.Sprintf("Exit: %v", err), err
			}
			return "Exit 0", nil
		case <-hc:
			ok := portOpen(sp.HealthTCPPort)
			s.mu.Lock()
			wasHealthy := st.Healthy
			st.Healthy = ok
			s.mu.Unlock()
			if ok {
				if !wasHealthy {
					s.log.Infof("supervisor: %s healthy (port %d open)", sp.Name, sp.HealthTCPPort)
				}
				misses = 0
				continue
			}
			if time.Now().Before(grace) {
				continue
			}
			misses++
			if misses >= 3 {
				s.log.Warnf("supervisor: %s: port %d closed for %d checks, restarting process", sp.Name, sp.HealthTCPPort, misses)
				stopChild(cmd, tree)
				<-done
				return "unhealthy (port closed)", fmt.Errorf("health check failed")
			}
		}
	}
}

// stopChild terminates the child and everything it started (Ollama -> llama-server). The direct kill
// afterwards applies if the child is not in the tree (tracking failed) - then only the main process, as before.
func stopChild(cmd *exec.Cmd, t *procTree) {
	t.kill()
	killChild(cmd)
}

// reapTree cleans up after a child that exited on its own: if Ollama crashes, its model process would otherwise
// keep running and hold VRAM, without anyone releasing it.
func (s *Supervisor) reapTree(name string, t *procTree) {
	n := t.alive()
	if n == 0 {
		return
	}
	t.kill()
	if n > 0 {
		s.log.Warnf("supervisor: %s: %d orphaned process(es) in the process tree ended", name, n)
	}
}

// childrenVRAM: VRAM (MiB) per running child except Ollama - its share is known to the router from /api/ps and the
// measurement; double-counting would be incorrect. nil if nothing to report or measurement unavailable (not Windows).
func (s *Supervisor) childrenVRAM() map[string]int {
	s.mu.Lock()
	trees := make(map[string]*procTree)
	for name, c := range s.children {
		if c.Running && c.tree != nil && !isOllamaExe(c.spec.Cmd) {
			trees[name] = c.tree
		}
	}
	s.mu.Unlock()
	if len(trees) == 0 {
		return nil
	}
	procVRAM, err := gpuProcessVRAM()
	if err != nil {
		return nil
	}
	out := make(map[string]int, len(trees))
	for name, t := range trees {
		var sum uint64
		for _, pid := range t.pids() {
			sum += procVRAM[pid]
		}
		out[name] = int(sum >> 20)
	}
	return out
}

// Child returns a copy of a child's state (for MQTT and /health).
func (s *Supervisor) Child(name string) (childState, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.children[name]
	if !ok {
		return childState{}, false
	}
	return *c, true
}

func (s *Supervisor) Status() map[string]childState {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]childState, len(s.children))
	for k, v := range s.children {
		out[k] = *v
	}
	return out
}

// Hold pauses a child and prevents restart until Release is called (Ollama update: swap files while
// nothing is running). Waits up to wait for the process tree to terminate; true = stopped.
func (s *Supervisor) Hold(name string, wait time.Duration) bool {
	s.mu.Lock()
	c, ok := s.children[name]
	if !ok {
		s.mu.Unlock()
		return false
	}
	c.held = true
	kill, running := c.kill, c.Running
	s.mu.Unlock()
	if running && kill != nil {
		kill()
	}
	deadline := time.Now().Add(wait)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		r := c.Running
		s.mu.Unlock()
		if !r {
			return true
		}
		time.Sleep(200 * time.Millisecond)
	}
	return false
}

// Release lifts Hold; the loop restarts the child immediately.
func (s *Supervisor) Release(name string) {
	s.mu.Lock()
	if c, ok := s.children[name]; ok {
		c.held = false
	}
	s.mu.Unlock()
}

// Park marks a child held before the supervisor starts it: it does not run until Release (the standby Ollama of
// tunnel.upstream_fallback, which runs only while the upstream is held). Call before Run.
func (s *Supervisor) Park(name string) {
	s.mu.Lock()
	if c, ok := s.children[name]; ok {
		c.held = true
	}
	s.mu.Unlock()
}

// withoutChild: the specs except the named child.
func withoutChild(specs []ChildSpec, name string) []ChildSpec {
	out := make([]ChildSpec, 0, len(specs))
	for _, sp := range specs {
		if sp.Name != name {
			out = append(out, sp)
		}
	}
	return out
}

// waitWhileHeld blocks while the child is held; false = ctx ended.
func (s *Supervisor) waitWhileHeld(ctx context.Context, name string) bool {
	for s.isHeld(name) {
		select {
		case <-ctx.Done():
			return false
		case <-time.After(300 * time.Millisecond):
		}
	}
	return true
}

func (s *Supervisor) isHeld(name string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	c, ok := s.children[name]
	return ok && c.held
}

// RestartChild terminates a child; the loop restarts it with backoff (verb restart-child, /health POST).
func (s *Supervisor) RestartChild(name string) bool {
	s.mu.Lock()
	c, ok := s.children[name]
	var kill func()
	if ok && c.Running {
		kill = c.kill
	}
	s.mu.Unlock()
	if kill == nil {
		return false
	}
	kill()
	return true
}
