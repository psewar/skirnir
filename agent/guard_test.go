package main

import (
	"errors"
	"testing"
	"time"
)

// Mock for the card: logs Set calls, can deny, "Reboot" resets the limit.
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

func TestGuardSetsSustainedLimitAndNeverHigher(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Date(2026, 9, 24, 22, 0, 0, 0, time.Local)
	st, evs := g.step(guardIn(t0, c, 30))
	if st.State != guardNormal || len(c.sets) != 1 || c.sets[0] != 460 || !hasEvent(evs, guardEvLimitSet) {
		t.Fatalf("Dauerlimit: state=%s sets=%v evs=%v", st.State, c.sets, evs)
	}
	if *st.TargetW != 460 || *st.LimitW != 460 || st.Problem {
		t.Fatalf("Status: %+v", st)
	}
	// Foreign tool sets lower: stays (never increase), no Set call
	c.lim.Cur = 400
	g.step(guardIn(t0.Add(40*time.Second), c, 30))
	if c.calls != 1 {
		t.Fatalf("tieferes Fremdlimit wurde angehoben: %d Aufrufe", c.calls)
	}
	// Absolute limit above default is clamped to default, below minimum to minimum
	g2 := newGuardEngine(GuardCfg{PowerLimitW: fptr(900)}, nil)
	d, s2 := g2.targets(c.lim)
	if d != 575 || s2 != 403 { // Stage 2 = 70 % of 575 = 402.5 -> 403, above Min 400
		t.Fatalf("Klemmung: dauer=%v stufe2=%v", d, s2)
	}
	g3 := newGuardEngine(GuardCfg{PowerLimitW: fptr(100)}, nil)
	if d, _ := g3.targets(c.lim); d != 400 {
		t.Fatalf("Klemmung unten: %v", d)
	}
}

func TestGuardRebootReappliesExactlyOnce(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	g.step(guardIn(t0, c, 30))
	c.lim.Cur = 575           // "Driver-Reset": the limit is back to default
	for i := 1; i <= 5; i++ { // before reapply_s (30 s) expires, no re-setting ...
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

func TestGuardRefusedBecomesUnavailable(t *testing.T) {
	c := newFakeCard()
	c.deny = errors.New("keine Berechtigung (rc 4)")
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	st, evs := g.step(guardIn(t0, c, 30))
	if st.State != guardUnavailable || !st.Problem || !hasEvent(evs, guardEvLimitRefused) {
		t.Fatalf("verweigert: %+v %v", st, evs)
	}
	st, evs = g.step(guardIn(t0.Add(2*time.Second), c, 30))
	if c.calls != 1 || hasEvent(evs, guardEvLimitRefused) {
		t.Fatalf("zweiter Versuch vor reapply_s oder doppeltes Ereignis: calls=%d evs=%v", c.calls, evs)
	}
	g.step(guardIn(t0.Add(31*time.Second), c, 30))
	if c.calls != 2 {
		t.Fatalf("nach reapply_s kein neuer Versuch: %d", c.calls)
	}
	// Someone else sets the limit correctly: no problem after reapply_s
	c.lim.Cur = 460
	st, _ = g.step(guardIn(t0.Add(70*time.Second), c, 30))
	if st.State != guardNormal || st.Problem {
		t.Fatalf("Limit stimmt, trotzdem Problem: %+v", st)
	}
}

func TestGuardHighLoadStage2Recovery(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	g.step(guardIn(t0, c, 30)) // 460 W gesetzt
	// just under 90 % of 460 (414): stays normal
	st, _ := g.step(guardIn(t0.Add(100*time.Second), c, 410))
	st, _ = g.step(guardIn(t0.Add(800*time.Second), c, 410))
	if st.State != guardNormal || st.HighLoadS != 0 {
		t.Fatalf("unter Schwelle: %+v", st)
	}
	// Full load: after 300 s warning, after 600 s Stage 2
	base := t0.Add(1000 * time.Second)
	st, _ = g.step(guardIn(base, c, 450))
	st, evs := g.step(guardIn(base.Add(301*time.Second), c, 450))
	if st.State != guardHighLoad || !hasEvent(evs, guardEvHighLoad) || st.HighLoadS < 300 {
		t.Fatalf("Hochlast: %+v %v", st, evs)
	}
	// short gap (under high_load_gap_s 15 s, e.g. queue between two requests) does not count as interruption
	st, _ = g.step(guardIn(base.Add(310*time.Second), c, 100))
	st, _ = g.step(guardIn(base.Add(320*time.Second), c, 450))
	if st.State != guardHighLoad || st.HighLoadS < 319 {
		t.Fatalf("kurze Luecke hat den Zaehler zurueckgesetzt: %+v", st)
	}
	// long interruption resets the counter
	st, _ = g.step(guardIn(base.Add(330*time.Second), c, 100))
	st, _ = g.step(guardIn(base.Add(350*time.Second), c, 100))
	if st.State != guardNormal || st.HighLoadS != 0 {
		t.Fatalf("Unterbrechung: %+v", st)
	}
	base = base.Add(400 * time.Second)
	g.step(guardIn(base, c, 455))
	st, evs = g.step(guardIn(base.Add(601*time.Second), c, 455))
	if st.State != guardThrottled || !hasEvent(evs, guardEvThrottled) || c.lim.Cur != 403 {
		t.Fatalf("Stufe 2: %+v %v limit=%v", st, evs, c.lim.Cur)
	}
	// during recovery time it stays at Stage 2, then back to 460 and 60 s "recovery"
	st, _ = g.step(guardIn(base.Add(801*time.Second), c, 380))
	if st.State != guardThrottled {
		t.Fatalf("zu frueh raus: %+v", st)
	}
	st, evs = g.step(guardIn(base.Add(902*time.Second), c, 380))
	if st.State != guardRecovery || !hasEvent(evs, guardEvRecovery) || c.lim.Cur != 460 {
		t.Fatalf("Erholung: %+v %v limit=%v", st, evs, c.lim.Cur)
	}
	st, _ = g.step(guardIn(base.Add(965*time.Second), c, 100))
	if st.State != guardNormal {
		t.Fatalf("nach Erholung: %+v", st)
	}
}

func TestGuardDeselectedIsProblem(t *testing.T) {
	c := newFakeCard()
	off := false
	g := newGuardEngine(GuardCfg{Enabled: &off}, c.set)
	st, evs := g.step(guardIn(time.Now(), c, 500))
	if st.State != guardOff || !st.Problem || st.Reason != guardEvDeselected || c.calls != 0 || !hasEvent(evs, guardEvDeselected) {
		t.Fatalf("aus: %+v calls=%d", st, c.calls)
	}
	if st.PowerW == nil || *st.PowerW != 500 {
		t.Fatal("Messwerte muessen auch bei aus mitkommen")
	}
}

func TestGuardWithoutLimitsUnavailable(t *testing.T) {
	g := newGuardEngine(GuardCfg{}, nil)
	st, _ := g.step(guardInput{Now: time.Now(), Limits: guardLimits{Err: "NVML nicht verfuegbar"}, PowerW: fptr(200)})
	if st.State != guardUnavailable || !st.Problem || st.Reason != "NVML nicht verfuegbar" {
		t.Fatalf("%+v", st)
	}
}

func TestGuardVoltageWarningAfterBaseline(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	// Learn reference: 10 idle samples one minute apart at 12.1 V
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
	// healthy drop (11.9 V, -0.2 V): no warning
	st, _ := load(base, 11.9)
	st, _ = load(base.Add(40*time.Second), 11.9)
	if len(st.Warnings) != 0 || st.Pin16RefV == nil || *st.Pin16RefV != 12.1 || st.Source != "nvml+gpuz" {
		t.Fatalf("gesund: %+v", st)
	}
	// Drop 0.5 V against reference: only after 30 s persistence
	st, evs := load(base.Add(60*time.Second), 11.6)
	if len(st.Warnings) != 0 || hasEvent(evs, guardEvVoltageLow) {
		t.Fatalf("zu frueh gewarnt: %+v", st)
	}
	st, evs = load(base.Add(91*time.Second), 11.6)
	if len(st.Warnings) != 1 || st.Warnings[0] != guardEvVoltageLow || !hasEvent(evs, guardEvVoltageLow) || !st.Problem {
		t.Fatalf("Spannungswarnung fehlt: %+v %v", st, evs)
	}
	// Reference stays clean: load samples must not alter it
	if *st.Pin16RefV != 12.1 {
		t.Fatalf("Referenz durch Last verfaelscht: %v", *st.Pin16RefV)
	}
	// Absolute threshold without reference: 11.5 V under load
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

func TestGuardTemperatureAndHardwareThrottle(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	in := guardIn(time.Now(), c, 300)
	in.MemTempC, in.Throttle = fptr(96), []string{"sw_power_cap", "hw_thermal"}
	st, evs := g.step(in)
	if len(st.Warnings) != 2 || !hasEvent(evs, guardEvTemperatureHigh) || !hasEvent(evs, guardEvHwThrottle) || !st.Problem {
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

func TestGuardExternalChange(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	g.step(guardIn(t0, c, 30))
	var evs []guardEvent
	var st GuardStatus
	for i := 1; i <= 3; i++ { // three times someone raises the limit again
		c.lim.Cur = 575
		st, evs = g.step(guardIn(t0.Add(time.Duration(i*31)*time.Second), c, 30))
	}
	if !hasEvent(evs, guardEvExternalChange) || len(st.Warnings) != 1 || st.Warnings[0] != guardEvExternalChange || c.lim.Cur != 460 {
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

// Review 2026-09-25: lastSetW lives only in memory. If the service restarts while the card is at Stage 2, the
// Guard must recognize its own target value and raise back to the sustained limit - a foreign value remains untouched.
func TestGuardRestartInStage2RaisesAgain(t *testing.T) {
	c := newFakeCard()
	c.lim.Cur = 403 // 70 % of 575: our Stage-2 value, set by the predecessor process
	g := newGuardEngine(GuardCfg{}, c.set)
	st, _ := g.step(guardIn(time.Now(), c, 30))
	if len(c.sets) != 1 || c.sets[0] != 460 || st.State != guardNormal {
		t.Fatalf("Stufe-2-Wert nach Neustart nicht angehoben: sets=%v state=%s", c.sets, st.State)
	}
	c2 := newFakeCard()
	c2.lim.Cur = 420 // Fremdtool
	g2 := newGuardEngine(GuardCfg{}, c2.set)
	g2.step(guardIn(time.Now(), c2, 30))
	if c2.calls != 0 {
		t.Fatalf("fremder Wert 420 W wurde angehoben: %d Aufrufe", c2.calls)
	}
}

// The 16-pin power comes via Relay from localhost (any local process could send it): it must not drive the
// high-load decision, only the Board Power from NVML.
func TestGuardHighLoadOnlyFromBoardPower(t *testing.T) {
	c := newFakeCard()
	g := newGuardEngine(GuardCfg{}, c.set)
	t0 := time.Now()
	g.step(guardIn(t0, c, 30))
	in := guardIn(t0.Add(5*time.Second), c, 100)
	in.Pin16W, in.GPUZ = fptr(560), true
	if st, _ := g.step(in); st.HighLoadS != 0 || st.State != guardNormal {
		t.Fatalf("gefaelschte 16-Pin-Leistung zaehlt als Hochlast: %+v", st)
	}
	g.step(guardIn(t0.Add(10*time.Second), c, 455)) // Board Power am Limit (460 W * 90 %): Zaehler startet
	if st, _ := g.step(guardIn(t0.Add(14*time.Second), c, 455)); st.HighLoadS != 4 {
		t.Fatalf("Board Power am Limit zaehlt nicht als Hochlast: %+v", st)
	}
}
