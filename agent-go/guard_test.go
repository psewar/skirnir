package main

import (
	"errors"
	"testing"
	"time"
)

// Attrappe fuer die Karte: protokolliert Set-Aufrufe, kann verweigern, "Reboot" setzt das Limit zurueck.
type fakeCard struct {
	lim   guardLimits
	sets  []float64
	deny  error
	calls int
}

func newFakeCard() *fakeCard {
	return &fakeCard{lim: guardLimits{Cur: 575, Def: 575, Min: 400, Max: 600, OK: true}}
}

func (c *fakeCard) set(w float64) error {
	c.calls++
	if c.deny != nil {
		return c.deny
	}
	c.sets = append(c.sets, w)
	c.lim.Cur = w
	return nil
}

func guardIn(now time.Time, c *fakeCard, powerW float64) guardInput {
	return guardInput{Now: now, Limits: c.lim, PowerW: fptr(powerW)}
}

func hasEvent(evs []guardEvent, t string) bool {
	for _, e := range evs {
		if e.Type == t {
			return true
		}
	}
	return false
}

func TestGuardSetztDauerlimitUndNieHoeher(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Date(2026, 9, 24, 22, 0, 0, 0, time.Local)
	st, evs := g.step(guardIn(t0, c, 30))
	if st.State != guardNormal || len(c.sets) != 1 || c.sets[0] != 460 || !hasEvent(evs, guardEvLimitGesetzt) {
		t.Fatalf("Dauerlimit: state=%s sets=%v evs=%v", st.State, c.sets, evs)
	}
	if *st.TargetW != 460 || *st.LimitW != 460 || st.Problem {
		t.Fatalf("Status: %+v", st)
	}
	// Fremdtool stellt tiefer: bleibt (nie erhoehen), kein Set-Aufruf
	c.lim.Cur = 400
	g.step(guardIn(t0.Add(40*time.Second), c, 30))
	if c.calls != 1 {
		t.Fatalf("tieferes Fremdlimit wurde angehoben: %d Aufrufe", c.calls)
	}
	// Absolutes Limit ueber dem Standard wird auf den Standard geklemmt, unter dem Minimum auf das Minimum
	g2 := newGuardEngine(GuardCfg{PowerLimitW: fptr(900)}, nil)
	d, s2 := g2.targets(c.lim)
	if d != 575 || s2 != 403 { // Stufe 2 = 70 % von 575 = 402.5 -> 403, ueber Min 400
		t.Fatalf("Klemmung: dauer=%v stufe2=%v", d, s2)
	}
	g3 := newGuardEngine(GuardCfg{PowerLimitW: fptr(100)}, nil)
	if d, _ := g3.targets(c.lim); d != 400 {
		t.Fatalf("Klemmung unten: %v", d)
	}
}

func TestGuardRebootSetztGenauEinmalNach(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	g.step(guardIn(t0, c, 30))
	c.lim.Cur = 575           // "Treiber-Reset": das Limit steht wieder auf dem Standard
	for i := 1; i <= 5; i++ { // vor Ablauf von reapply_s (30 s) kein Nachsetzen ...
		g.step(guardIn(t0.Add(time.Duration(i*2)*time.Second), c, 30))
	}
	if c.calls != 1 {
		t.Fatalf("vor reapply_s nachgesetzt: %d Aufrufe", c.calls)
	}
	for i := 31; i <= 50; i += 2 { // ... danach genau einmal
		g.step(guardIn(t0.Add(time.Duration(i)*time.Second), c, 30))
	}
	if c.calls != 2 || c.lim.Cur != 460 {
		t.Fatalf("Nachsetzen: %d Aufrufe, Limit %v", c.calls, c.lim.Cur)
	}
}

func TestGuardVerweigertWirdUnverfuegbar(t *testing.T) {
	c := newFakeCard()
	c.deny = errors.New("keine Berechtigung (rc 4)")
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	st, evs := g.step(guardIn(t0, c, 30))
	if st.State != guardUnverfuegbar || !st.Problem || !hasEvent(evs, guardEvLimitVerweigert) {
		t.Fatalf("verweigert: %+v %v", st, evs)
	}
	st, evs = g.step(guardIn(t0.Add(2*time.Second), c, 30))
	if c.calls != 1 || hasEvent(evs, guardEvLimitVerweigert) {
		t.Fatalf("zweiter Versuch vor reapply_s oder doppeltes Ereignis: calls=%d evs=%v", c.calls, evs)
	}
	g.step(guardIn(t0.Add(31*time.Second), c, 30))
	if c.calls != 2 {
		t.Fatalf("nach reapply_s kein neuer Versuch: %d", c.calls)
	}
	// Jemand anders setzt das Limit richtig: nach reapply_s kein Problem mehr
	c.lim.Cur = 460
	st, _ = g.step(guardIn(t0.Add(70*time.Second), c, 30))
	if st.State != guardNormal || st.Problem {
		t.Fatalf("Limit stimmt, trotzdem Problem: %+v", st)
	}
}

func TestGuardHochlastStufe2Erholung(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	g.step(guardIn(t0, c, 30)) // 460 W gesetzt
	// knapp unter 90 % von 460 (414): bleibt normal
	st, _ := g.step(guardIn(t0.Add(100*time.Second), c, 410))
	st, _ = g.step(guardIn(t0.Add(800*time.Second), c, 410))
	if st.State != guardNormal || st.HighLoadS != 0 {
		t.Fatalf("unter Schwelle: %+v", st)
	}
	// Vollast: nach 300 s Vorwarnung, nach 600 s Stufe 2
	base := t0.Add(1000 * time.Second)
	st, _ = g.step(guardIn(base, c, 450))
	st, evs := g.step(guardIn(base.Add(301*time.Second), c, 450))
	if st.State != guardHochlast || !hasEvent(evs, guardEvHochlast) || st.HighLoadS < 300 {
		t.Fatalf("Hochlast: %+v %v", st, evs)
	}
	// kurze Luecke (unter high_load_gap_s 15 s, z. B. Warteschlange zwischen zwei Anfragen) zaehlt nicht als Unterbrechung
	st, _ = g.step(guardIn(base.Add(310*time.Second), c, 100))
	st, _ = g.step(guardIn(base.Add(320*time.Second), c, 450))
	if st.State != guardHochlast || st.HighLoadS < 319 {
		t.Fatalf("kurze Luecke hat den Zaehler zurueckgesetzt: %+v", st)
	}
	// lange Unterbrechung setzt den Zaehler zurueck
	st, _ = g.step(guardIn(base.Add(330*time.Second), c, 100))
	st, _ = g.step(guardIn(base.Add(350*time.Second), c, 100))
	if st.State != guardNormal || st.HighLoadS != 0 {
		t.Fatalf("Unterbrechung: %+v", st)
	}
	base = base.Add(400 * time.Second)
	g.step(guardIn(base, c, 455))
	st, evs = g.step(guardIn(base.Add(601*time.Second), c, 455))
	if st.State != guardGedrosselt || !hasEvent(evs, guardEvGedrosselt) || c.lim.Cur != 403 {
		t.Fatalf("Stufe 2: %+v %v limit=%v", st, evs, c.lim.Cur)
	}
	// waehrend der Erholungszeit bleibt es bei Stufe 2, danach zurueck auf 460 und 60 s "erholung"
	st, _ = g.step(guardIn(base.Add(801*time.Second), c, 380))
	if st.State != guardGedrosselt {
		t.Fatalf("zu frueh raus: %+v", st)
	}
	st, evs = g.step(guardIn(base.Add(902*time.Second), c, 380))
	if st.State != guardErholung || !hasEvent(evs, guardEvErholung) || c.lim.Cur != 460 {
		t.Fatalf("Erholung: %+v %v limit=%v", st, evs, c.lim.Cur)
	}
	st, _ = g.step(guardIn(base.Add(965*time.Second), c, 100))
	if st.State != guardNormal {
		t.Fatalf("nach Erholung: %+v", st)
	}
}

func TestGuardAbgewaehltIstProblem(t *testing.T) {
	c := newFakeCard()
	off := false
	g := newGuardEngine(GuardCfg{Enabled: &off}, c.set)
	st, evs := g.step(guardIn(time.Now(), c, 500))
	if st.State != guardAus || !st.Problem || st.Reason != "abgewaehlt" || c.calls != 0 || !hasEvent(evs, guardEvAbgewaehlt) {
		t.Fatalf("aus: %+v calls=%d", st, c.calls)
	}
	if st.PowerW == nil || *st.PowerW != 500 {
		t.Fatal("Messwerte muessen auch bei aus mitkommen")
	}
}

func TestGuardOhneLimitsUnverfuegbar(t *testing.T) {
	g := newGuardEngine(GuardCfg{}, nil)
	st, _ := g.step(guardInput{Now: time.Now(), Limits: guardLimits{Err: "NVML nicht verfuegbar"}, PowerW: fptr(200)})
	if st.State != guardUnverfuegbar || !st.Problem || st.Reason != "NVML nicht verfuegbar" {
		t.Fatalf("%+v", st)
	}
}

func TestGuardSpannungswarnungNachBestand(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	// Referenz lernen: 10 Leerlaufproben je eine Minute auseinander bei 12.1 V
	for i := 0; i < 10; i++ {
		in := guardIn(t0.Add(time.Duration(i)*time.Minute), c, 25)
		in.Pin16W, in.Pin16V = fptr(22), fptr(12.1)
		g.step(in)
	}
	base := t0.Add(20 * time.Minute)
	load := func(at time.Time, v float64) (GuardStatus, []guardEvent) {
		in := guardIn(at, c, 440)
		in.Pin16W, in.Pin16V, in.GPUZ = fptr(430), fptr(v), true
		return g.step(in)
	}
	// gesunder Abfall (11.9 V, -0.2 V): keine Warnung
	st, _ := load(base, 11.9)
	st, _ = load(base.Add(40*time.Second), 11.9)
	if len(st.Warnings) != 0 || st.Pin16RefV == nil || *st.Pin16RefV != 12.1 || st.Source != "nvml+gpuz" {
		t.Fatalf("gesund: %+v", st)
	}
	// Abfall 0.5 V gegen Referenz: erst nach 30 s Bestand
	st, evs := load(base.Add(60*time.Second), 11.6)
	if len(st.Warnings) != 0 || hasEvent(evs, guardEvSpannung) {
		t.Fatalf("zu frueh gewarnt: %+v", st)
	}
	st, evs = load(base.Add(91*time.Second), 11.6)
	if len(st.Warnings) != 1 || st.Warnings[0] != guardEvSpannung || !hasEvent(evs, guardEvSpannung) || !st.Problem {
		t.Fatalf("Spannungswarnung fehlt: %+v %v", st, evs)
	}
	// Referenz bleibt sauber: Lastproben duerfen sie nicht veraendern
	if *st.Pin16RefV != 12.1 {
		t.Fatalf("Referenz durch Last verfaelscht: %v", *st.Pin16RefV)
	}
	// Absolutschwelle ohne Referenz: 11.5 V unter Last
	g2 := newGuardEngine(GuardCfg{}, c.set)
	in := guardIn(base, c, 440)
	in.Pin16W, in.Pin16V = fptr(430), fptr(11.5)
	g2.step(in)
	in.Now = base.Add(31 * time.Second)
	st, _ = g2.step(in)
	if len(st.Warnings) != 1 {
		t.Fatalf("Absolutschwelle: %+v", st)
	}
}

func TestGuardTemperaturUndHardwareDrossel(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	in := guardIn(time.Now(), c, 300)
	in.MemTempC, in.Throttle = fptr(96), []string{"sw_power_cap", "hw_thermal"}
	st, evs := g.step(in)
	if len(st.Warnings) != 2 || !hasEvent(evs, guardEvTemperatur) || !hasEvent(evs, guardEvHwDrossel) || !st.Problem {
		t.Fatalf("%+v %v", st, evs)
	}
	in.Now = in.Now.Add(2 * time.Second)
	_, evs = g.step(in)
	if len(evs) != 0 {
		t.Fatalf("Warnereignisse duerfen nicht jede Messung wiederholen: %v", evs)
	}
	in.MemTempC, in.Throttle = fptr(80), nil
	st, _ = g.step(in)
	if len(st.Warnings) != 0 || st.Problem {
		t.Fatalf("Warnung bleibt haengen: %+v", st)
	}
}

func TestGuardFremdeingriff(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	g.step(guardIn(t0, c, 30))
	var evs []guardEvent
	var st GuardStatus
	for i := 1; i <= 3; i++ { // dreimal hebt jemand das Limit wieder an
		c.lim.Cur = 575
		st, evs = g.step(guardIn(t0.Add(time.Duration(i*31)*time.Second), c, 30))
	}
	if !hasEvent(evs, guardEvFremdeingriff) || len(st.Warnings) != 1 || st.Warnings[0] != guardEvFremdeingriff || c.lim.Cur != 460 {
		t.Fatalf("Fremdeingriff: %+v %v", st, evs)
	}
}

func TestGuardInputFromSample(t *testing.T) {
	s := &GPUSample{Sensors: &GPUSensors{PowerW: fptr(200), Pin16PowerW: fptr(190), Pin16VoltageV: fptr(11.9), MemTempC: fptr(60), GPUZ: true, ThrottleReasons: []string{"idle"}}}
	in := inputFrom(time.Now(), s, guardLimits{OK: true, Cur: 575, Def: 575})
	if *in.PowerW != 200 || *in.Pin16W != 190 || *in.Pin16V != 11.9 || *in.MemTempC != 60 || !in.GPUZ || len(in.Throttle) != 1 {
		t.Fatalf("%+v", in)
	}
	if in2 := inputFrom(time.Now(), nil, guardLimits{}); in2.PowerW != nil {
		t.Fatal("nil-Sample")
	}
}
