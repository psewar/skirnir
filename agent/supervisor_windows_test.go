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

// Nachgestellt wird der Vorfall vom 2026-09-23: Ollama startet sein Modell als eigenen Prozess
// (llama-server). Der Supervisor startete Ollama neu, beendete dabei aber nur ollama.exe - der
// Modellprozess ueberlebte als Waise mit 23,5 GiB Grafikspeicher und hielt den Knoten stundenlang "busy".
//
// Hier spielt die Testdatei selbst beide Rollen: ein "Elternteil" (Ollama) startet einen "Enkel"
// (llama-server), meldet dessen PID und wartet dann.

const helperEnv = "SKIRNIR_TEST_HELFER"

// TestHelferProzess ist kein Test, sondern der Rollenwechsel fuer die Kindprozesse.
func TestHelferProzess(t *testing.T) {
	switch os.Getenv(helperEnv) {
	case "":
		t.Skip("nur als Kindprozess")
	case "enkel":
		time.Sleep(2 * time.Minute)
		os.Exit(0)
	case "sofort":
		bufio.NewReader(os.Stdin).ReadString('\n') // erst nach dem Einhaengen enden (kein Wettrennen)
		os.Exit(0)
	case "eltern", "eltern-kurz":
		// Erst loslegen, wenn der Test das Elternteil in den Baum gehaengt hat - sonst liefe der Enkel
		// ausserhalb des Jobs, und der Test wuerde etwas anderes pruefen als den Supervisor.
		bufio.NewReader(os.Stdin).ReadString('\n')
		enkel := exec.Command(os.Args[0], "-test.run=^TestHelferProzess$")
		enkel.Env = append(os.Environ(), helperEnv+"=enkel")
		if err := enkel.Start(); err != nil {
			fmt.Println("FEHLER", err)
			os.Exit(3)
		}
		fmt.Println("ENKEL", enkel.Process.Pid)
		if os.Getenv(helperEnv) == "eltern-kurz" {
			os.Exit(0) // "Absturz": das Elternteil geht, der Enkel bleibt
		}
		time.Sleep(2 * time.Minute)
		os.Exit(0)
	}
}

// lebt prueft, ob ein Prozess noch laeuft (WaitForSingleObject mit Zeitlimit 0).
func lebt(pid int) bool {
	h, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false // gibt es nicht mehr
	}
	defer windows.CloseHandle(h)
	ev, _ := windows.WaitForSingleObject(h, 0)
	return ev == uint32(windows.WAIT_TIMEOUT)
}

func wartetBisTot(pid int, frist time.Duration) bool {
	ende := time.Now().Add(frist)
	for time.Now().Before(ende) {
		if !lebt(pid) {
			return true
		}
		time.Sleep(50 * time.Millisecond)
	}
	return !lebt(pid)
}

// starteFamilie startet ein Elternteil, haengt es in einen Baum und liefert die PID des Enkels.
func starteFamilie(t *testing.T, g *childGroup, rolle string) (*exec.Cmd, *procTree, int) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestHelferProzess$")
	cmd.Env = append(os.Environ(), helperEnv+"="+rolle)
	cmd.SysProcAttr = childProcAttr()
	ein, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	aus, err := cmd.StdoutPipe()
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
	fmt.Fprintln(ein, "los")
	sc := bufio.NewScanner(aus)
	for sc.Scan() {
		if f := strings.Fields(sc.Text()); len(f) == 2 && f[0] == "ENKEL" {
			pid, _ := strconv.Atoi(f[1])
			if !lebt(pid) {
				t.Fatalf("Enkel %d lebt nicht einmal nach dem Start", pid)
			}
			return cmd, tree, pid
		}
	}
	t.Fatal("Elternteil hat keine Enkel-PID gemeldet")
	return nil, nil, 0
}

func neueGruppe(t *testing.T) *childGroup {
	t.Helper()
	g, err := newChildGroup()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { windows.CloseHandle(g.job) }) // KILL_ON_JOB_CLOSE raeumt Reste weg
	return g
}

// Gegenprobe: so verhielt sich der Agent bis 0.5.1. Nur der Hauptprozess stirbt, der Enkel lebt weiter.
// Faellt DIESER Test um, prueft die Anordnung nicht mehr, was sie soll.
func TestNurHauptprozessBeendenLaesstEnkelLeben(t *testing.T) {
	g := neueGruppe(t)
	cmd, tree, enkel := starteFamilie(t, g, "eltern")
	defer tree.release()
	defer tree.kill()

	killChild(cmd) // der alte Weg
	cmd.Wait()
	if wartetBisTot(enkel, 2*time.Second) {
		t.Fatalf("Enkel %d ist mitgestorben - die Gegenprobe zeigt den alten Fehler nicht mehr", enkel)
	}
}

// Der Fehler vom 2026-09-23: ein Neustart muss den ganzen Baum beenden.
func TestNeustartBeendetAuchDenEnkel(t *testing.T) {
	g := neueGruppe(t)
	cmd, tree, enkel := starteFamilie(t, g, "eltern")
	defer tree.release()

	stopChild(cmd, tree)
	cmd.Wait()
	if !wartetBisTot(enkel, 5*time.Second) {
		tree.kill()
		t.Fatalf("Enkel %d lebt nach dem Neustart weiter - genau der Waisen-Fehler", enkel)
	}
}

// Stuerzt das Elternteil von selbst ab, raeumt reapTree den verwaisten Enkel weg und meldet ihn.
func TestAbsturzHinterlaesstKeineWaise(t *testing.T) {
	g := neueGruppe(t)
	cmd, tree, enkel := starteFamilie(t, g, "eltern-kurz")
	defer tree.release()
	cmd.Wait() // das Elternteil ist weg, der Enkel noch da

	if !lebt(enkel) {
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
	t.Cleanup(func() { log.file.Close() }) // Windows loescht keine offene Datei
	s := &Supervisor{log: log}
	s.reapTree("ollama", tree)

	if !wartetBisTot(enkel, 5*time.Second) {
		t.Fatalf("verwaister Enkel %d lebt nach reapTree weiter", enkel)
	}
	inhalt := logInhalt(t, dir)
	if !strings.Contains(inhalt, "ollama: 1 verwaiste(r) Prozess(e) im Prozessbaum beendet") {
		t.Fatalf("Aufraeumen nicht im Log vermerkt:\n%s", inhalt)
	}
}

// Ein Kind, das sauber endet und nichts zuruecklaesst, darf keine Warnung erzeugen.
func TestSauberesEndeOhneWarnung(t *testing.T) {
	g := neueGruppe(t)
	cmd := exec.Command(os.Args[0], "-test.run=^TestHelferProzess$")
	cmd.Env = append(os.Environ(), helperEnv+"=sofort")
	cmd.SysProcAttr = childProcAttr()
	ein, err := cmd.StdinPipe()
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
	fmt.Fprintln(ein, "los")
	cmd.Wait()

	dir := t.TempDir()
	log, err := newLogger(LogCfg{Dir: dir}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() })
	(&Supervisor{log: log}).reapTree("stt", tree)
	if inhalt := logInhalt(t, dir); strings.Contains(inhalt, "verwaist") {
		t.Fatalf("Warnung ohne Waise:\n%s", inhalt)
	}
}

func logInhalt(t *testing.T, dir string) string {
	t.Helper()
	var alles strings.Builder
	filepath.Walk(dir, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			b, _ := os.ReadFile(p)
			alles.Write(b)
		}
		return nil
	})
	return alles.String()
}
