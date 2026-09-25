package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"sync"
	"time"
)

// Supervisor startet und ueberwacht Kindprozesse (Ollama, Wyoming-STT). Windows: alle Kinder haengen an einem
// Job-Objekt mit KILL_ON_JOB_CLOSE - stirbt der Dienst, sterben sie mit - und jedes Kind zusaetzlich an einem
// eigenen Unter-Job (procTree), damit ein Neustart auch seine Enkel beendet. Linux: eigene Prozessgruppe.
type Supervisor struct {
	specs []ChildSpec
	logc  LogCfg
	log   *Logger
	group *childGroup

	mu       sync.Mutex
	children map[string]*childState

	kuerzungen *Kuerzungen // stille Kuerzungen im Ollama-Log (kuerzung.go)
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
}

var restartBackoff = []time.Duration{time.Second, 5 * time.Second, 30 * time.Second, 60 * time.Second}

func newSupervisor(specs []ChildSpec, logc LogCfg, log *Logger) (*Supervisor, error) {
	g, err := newChildGroup()
	if err != nil {
		return nil, err
	}
	s := &Supervisor{specs: specs, logc: logc, log: log, group: g, children: map[string]*childState{},
		kuerzungen: newKuerzungen()}
	for _, sp := range specs {
		s.children[sp.Name] = &childState{spec: sp}
	}
	return s, nil
}

func (s *Supervisor) Run(ctx context.Context) {
	var wg sync.WaitGroup
	for _, sp := range s.specs {
		if !sp.on() {
			s.log.Infof("supervisor: Kind %s deaktiviert", sp.Name)
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
		if s.kuerzungen.pruefe(name, z) {
			s.log.Warnf("supervisor: %s hat einen Prompt gekuerzt: %s", name, z)
		}
	}
	attempt := 0
	for {
		start := time.Now()
		exitMsg, err := s.runOnce(ctx, sp, st, out)
		if ctx.Err() != nil {
			return
		}
		ran := time.Since(start)
		if ran > 5*time.Minute {
			attempt = 0 // lief lange stabil: Backoff zuruecksetzen
		}
		delay := restartBackoff[min(attempt, len(restartBackoff)-1)]
		attempt++
		s.mu.Lock()
		st.Running, st.Healthy, st.PID = false, false, 0
		st.LastExit = fmt.Sprintf("%s nach %s", exitMsg, ran.Round(time.Second))
		st.Restarts++
		st.NextStart = time.Now().Add(delay)
		s.mu.Unlock()
		if err != nil {
			s.log.Warnf("supervisor: %s: %s, Neustart in %s", sp.Name, exitMsg, delay)
		} else {
			s.log.Warnf("supervisor: %s beendet (%s), Neustart in %s", sp.Name, exitMsg, delay)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
	}
}

// runOnce startet das Kind, haengt es an die Gruppe, prueft die Gesundheit und wartet auf das Ende.
func (s *Supervisor) runOnce(ctx context.Context, sp ChildSpec, st *childState, out *childWriter) (string, error) {
	cmd := exec.Command(sp.Cmd, sp.Args...)
	cmd.Dir = sp.Cwd
	cmd.Env = append(os.Environ(), sp.Env...)
	cmd.Stdout, cmd.Stderr = out, out
	cmd.SysProcAttr = childProcAttr()
	if err := cmd.Start(); err != nil {
		return fmt.Sprintf("Start fehlgeschlagen: %v", err), err
	}
	tree, err := s.group.track(cmd.Process.Pid)
	if err != nil {
		s.log.Warnf("supervisor: %s: Prozessgruppe: %v", sp.Name, err)
	}
	// Handle am Ende freigeben; unter Windows beendet das (KILL_ON_JOB_CLOSE) auch, was noch uebrig ist.
	defer tree.release()
	s.mu.Lock()
	st.Running, st.Healthy, st.PID, st.Since, st.NextStart = true, sp.HealthTCPPort == 0, cmd.Process.Pid, time.Now(), time.Time{}
	st.kill = func() { stopChild(cmd, tree) }
	s.mu.Unlock()
	s.log.Infof("supervisor: %s gestartet (PID %d): %s %v", sp.Name, cmd.Process.Pid, sp.Cmd, sp.Args)

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	// Gesundheitspruefung: nach der Gnadenfrist muss der Port offen sein, sonst 3 Fehlversuche -> Neustart
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
					s.log.Infof("supervisor: %s gesund (Port %d offen)", sp.Name, sp.HealthTCPPort)
				}
				misses = 0
				continue
			}
			if time.Now().Before(grace) {
				continue
			}
			misses++
			if misses >= 3 {
				s.log.Warnf("supervisor: %s: Port %d seit %d Pruefungen zu, Prozess wird neu gestartet", sp.Name, sp.HealthTCPPort, misses)
				stopChild(cmd, tree)
				<-done
				return "ungesund (Port zu)", fmt.Errorf("health check failed")
			}
		}
	}
}

// stopChild beendet das Kind samt allem, was es gestartet hat (Ollama -> llama-server). Der direkte Kill
// danach greift, falls das Kind nicht im Baum haengt (track gescheitert) - dann wie frueher nur der Hauptprozess.
func stopChild(cmd *exec.Cmd, t *procTree) {
	t.kill()
	killChild(cmd)
}

// reapTree raeumt nach einem Kind auf, das von selbst geendet hat: stuerzt Ollama ab, lebt sein
// Modellprozess sonst weiter und haelt den Grafikspeicher fest, ohne dass ihn jemand wieder freigibt.
func (s *Supervisor) reapTree(name string, t *procTree) {
	n := t.alive()
	if n == 0 {
		return
	}
	t.kill()
	if n > 0 {
		s.log.Warnf("supervisor: %s: %d verwaiste(r) Prozess(e) im Prozessbaum beendet", name, n)
	}
}

// Child liefert eine Kopie des Zustands eines Kindes (fuer MQTT und /health).
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

// RestartChild beendet ein Kind; die Schleife startet es mit Backoff neu (Verb restart-child, /health POST).
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
