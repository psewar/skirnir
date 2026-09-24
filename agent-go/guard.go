package main

import (
	"context"
	"fmt"
	"math"
	"sort"
	"sync"
	"time"
)

// GPU-Schutz (gpu_guard, seit 0.7.0; Entwurf design/gpu-guard.md): Der 12V-2x6-Stecker einer RTX 5090 fuehrt bei 575 W
// rund 48 A ueber sechs Adern ohne Einzelpin-Messung. Software sieht keinen schlechten Kontakt, kann aber den Gesamtstrom
// senken (Power-Limit), Dauer-Vollast begrenzen und frueh warnen (Spannungsabfall am Stecker, Speichertemperatur).
//
// Der Kern ist eine reine Funktion (guardEngine.step) ueber Messwerte und Zeit - testbar ohne Karte. Das Setzen des
// Limits geht ueber die eingehaengten Funktionen limits/setLimit (NVML unter Windows, nvidia-smi sonst). Der Guard
// senkt nur, erhoeht nie ueber das Standardlimit, und klemmt jedes Ziel auf [Min-Limit, Standardlimit].
//
// Zustaende: aus | unverfuegbar | normal | hochlast | gedrosselt | erholung. Die Abwahl (enabled: false) gilt auf
// Wunsch des Betreibers als Problem (HA), damit eine vergessene Abwahl auffaellt.

const (
	guardAus          = "aus"
	guardUnverfuegbar = "unverfuegbar"
	guardNormal       = "normal"
	guardHochlast     = "hochlast"
	guardGedrosselt   = "gedrosselt"
	guardErholung     = "erholung"

	guardEvLimitGesetzt    = "limit_gesetzt"
	guardEvLimitVerweigert = "limit_verweigert"
	guardEvFremdeingriff   = "fremdeingriff"
	guardEvHochlast        = "hochlast"
	guardEvGedrosselt      = "gedrosselt"
	guardEvErholung        = "erholung"
	guardEvSpannung        = "spannung_niedrig"
	guardEvTemperatur      = "temperatur_hoch"
	guardEvHwDrossel       = "hw_drossel"
	guardEvAbgewaehlt      = "abgewaehlt"

	guardErholungMeldenS = 60.0
	guardVRefMaxProben   = 1440 // eine Leerlauf-Spannungsprobe je Minute, 24 h
)

// GuardCfg: Block gpu_guard in der Agent-Konfiguration. Fehlende Werte = Standard (guardDefaults).
type GuardCfg struct {
	Enabled       *bool    `yaml:"enabled" json:"enabled,omitempty"`
	PowerLimitPct *float64 `yaml:"power_limit_pct" json:"power_limit_pct,omitempty"` // Dauerlimit in % des Standardlimits
	PowerLimitW   *float64 `yaml:"power_limit_w" json:"power_limit_w,omitempty"`     // alternativ absolut; gewinnt vor pct
	ReapplyS      *float64 `yaml:"reapply_s" json:"reapply_s,omitempty"`
	HighLoadPct   *float64 `yaml:"high_load_pct" json:"high_load_pct,omitempty"` // Hochlast ab % des aktiven Limits ...
	HighLoadS     *float64 `yaml:"high_load_s" json:"high_load_s,omitempty"`     // ... ununterbrochen so lange -> Stufe 2
	Stage2Pct     *float64 `yaml:"stage2_pct" json:"stage2_pct,omitempty"`       // Limit in Stufe 2 (% des Standardlimits)
	RecoveryS     *float64 `yaml:"recovery_s" json:"recovery_s,omitempty"`       // Dauer von Stufe 2
	VoltageWarnV  *float64 `yaml:"voltage_warn_v" json:"voltage_warn_v,omitempty"`
	VoltageDropV  *float64 `yaml:"voltage_drop_v" json:"voltage_drop_v,omitempty"`
	VoltageHoldS  *float64 `yaml:"voltage_hold_s" json:"voltage_hold_s,omitempty"`
	VoltageLoadW  *float64 `yaml:"voltage_load_w" json:"voltage_load_w,omitempty"` // Spannung zaehlt nur ab dieser 16-Pin-Leistung
	MemTempWarnC  *float64 `yaml:"mem_temp_warn_c" json:"mem_temp_warn_c,omitempty"`
	HotspotWarnC  *float64 `yaml:"hotspot_warn_c" json:"hotspot_warn_c,omitempty"`
}

func (c *GuardCfg) on() bool { return c.Enabled == nil || *c.Enabled }

// guardParams: aufgeloeste Werte.
type guardParams struct {
	enabled                                    bool
	limitPct, limitW, reapplyS                 float64
	highPct, highS, stage2Pct, recoveryS       float64
	voltWarnV, voltDropV, voltHoldS, voltLoadW float64
	memWarnC, hotWarnC                         float64
}

func (c GuardCfg) resolved() guardParams {
	f := func(p *float64, d float64) float64 {
		if p != nil {
			return *p
		}
		return d
	}
	return guardParams{
		enabled: c.on(), limitPct: f(c.PowerLimitPct, 80), limitW: f(c.PowerLimitW, 0), reapplyS: f(c.ReapplyS, 30),
		highPct: f(c.HighLoadPct, 90), highS: f(c.HighLoadS, 600), stage2Pct: f(c.Stage2Pct, 70), recoveryS: f(c.RecoveryS, 300),
		voltWarnV: f(c.VoltageWarnV, 11.6), voltDropV: f(c.VoltageDropV, 0.35), voltHoldS: f(c.VoltageHoldS, 30), voltLoadW: f(c.VoltageLoadW, 300),
		memWarnC: f(c.MemTempWarnC, 95), hotWarnC: f(c.HotspotWarnC, 100),
	}
}

// guardLimits: was die Karte ueber ihr Limit sagt (Watt). ok=false = nicht lesbar (kein NVML, Karte ohne Limit).
type guardLimits struct {
	Cur, Def, Min, Max float64
	OK                 bool
	Err                string
}

// guardInput: eine Messung.
type guardInput struct {
	Now      time.Time
	Limits   guardLimits
	PowerW   *float64 // Board Power (NVML)
	Pin16W   *float64 // GPU-Z
	Pin16V   *float64 // GPU-Z
	MemTempC *float64
	HotspotC *float64
	Throttle []string
	GPUZ     bool
}

// GuardStatus geht in Heartbeat, /health und MQTT.
type GuardStatus struct {
	State         string   `json:"state"`
	Source        string   `json:"quelle"`
	PowerW        *float64 `json:"power_w,omitempty"`
	Pin16W        *float64 `json:"pin16_w,omitempty"`
	Pin16V        *float64 `json:"pin16_v,omitempty"`
	Pin16RefV     *float64 `json:"pin16_ref_v,omitempty"`
	LimitW        *float64 `json:"limit_w,omitempty"`
	DefaultLimitW *float64 `json:"default_limit_w,omitempty"`
	TargetW       *float64 `json:"target_w,omitempty"`
	Throttle      []string `json:"throttle,omitempty"`
	HighLoadS     int      `json:"hochlast_s"`
	Problem       bool     `json:"problem"`
	Reason        string   `json:"grund,omitempty"`
	Warnings      []string `json:"warnungen,omitempty"`
}

type guardEvent struct {
	Type   string    `json:"event_type"`
	Msg    string    `json:"msg"`
	LimitW float64   `json:"limit_w,omitempty"`
	At     time.Time `json:"zeit"`
}

// guardEngine haelt den Zustand zwischen zwei Messungen. Nicht nebenlaeufig sicher - der Aufrufer serialisiert.
type guardEngine struct {
	p        guardParams
	setLimit func(w float64) error // nil = kein Hebel (Tests: Attrappe)

	state         string
	highSince     time.Time
	stage2Since   time.Time
	recoverySince time.Time
	lastApply     time.Time
	lastWant      float64 // zuletzt angestrebter Wert (Wechsel -> sofort anwenden, sonst alle reapply_s)
	lastSetW      float64 // was wir zuletzt erfolgreich gesetzt haben (0 = nie)
	conflicts     int
	denied        string
	deniedAt      time.Time
	voltLowSince  time.Time
	voltWarned    bool
	tempWarned    bool
	hwWarned      bool
	fremdWarned   bool
	vref          []float64 // Leerlauf-Spannungsproben
	vrefLast      time.Time
}

func newGuardEngine(cfg GuardCfg, setLimit func(float64) error) *guardEngine {
	return &guardEngine{p: cfg.resolved(), setLimit: setLimit, state: guardNormal}
}

func clampf(v, lo, hi float64) float64 { return math.Max(lo, math.Min(hi, v)) }

// targets: Dauerlimit und Stufe-2-Limit, geklemmt auf [Min, Standard]. Ueber den Standard geht es nie.
func (g *guardEngine) targets(l guardLimits) (dauer, stufe2 float64) {
	lo := l.Min
	if lo <= 0 {
		lo = l.Def * 0.5
	}
	hi := l.Def
	if g.p.limitW > 0 {
		dauer = g.p.limitW
	} else {
		dauer = l.Def * g.p.limitPct / 100
	}
	dauer = clampf(math.Round(dauer), lo, hi)
	stufe2 = clampf(math.Round(l.Def*g.p.stage2Pct/100), lo, dauer)
	return dauer, stufe2
}

func (g *guardEngine) vrefMedian() *float64 {
	if len(g.vref) < 5 {
		return nil
	}
	c := append([]float64(nil), g.vref...)
	sort.Float64s(c)
	return fptr(math.Round(c[len(c)/2]*1000) / 1000)
}

// step wertet eine Messung aus, setzt bei Bedarf das Limit (ueber setLimit) und liefert Status und Ereignisse.
func (g *guardEngine) step(in guardInput) (GuardStatus, []guardEvent) {
	var evs []guardEvent
	ev := func(t, msg string, w float64) {
		evs = append(evs, guardEvent{Type: t, Msg: msg, LimitW: w, At: in.Now})
	}
	st := GuardStatus{Source: "nvml", PowerW: in.PowerW, Pin16W: in.Pin16W, Pin16V: in.Pin16V, Throttle: in.Throttle}
	if in.GPUZ {
		st.Source = "nvml+gpuz"
	}
	if in.Limits.OK {
		st.LimitW, st.DefaultLimitW = fptr(in.Limits.Cur), fptr(in.Limits.Def)
	}

	// Warnungen, die unabhaengig vom Limit gelten (auch bei aus/unverfuegbar): Spannung, Temperatur, Hardware-Drossel.
	g.watchVoltage(in, &st, ev)
	g.watchTemperature(in, &st, ev)
	g.watchHardware(in, &st, ev)

	if !g.p.enabled {
		if g.state != guardAus {
			ev(guardEvAbgewaehlt, "GPU-Schutz per Konfiguration abgewaehlt - kein Limit, nur Messwerte", 0)
		}
		g.state = guardAus
		st.State, st.Problem, st.Reason = guardAus, true, "abgewaehlt"
		return st, evs
	}
	if !in.Limits.OK || in.Limits.Def <= 0 {
		g.state = guardUnverfuegbar
		st.State, st.Problem, st.Reason = guardUnverfuegbar, true, firstNonEmpty(in.Limits.Err, "Power-Limit nicht lesbar")
		return st, evs
	}
	dauer, stufe2 := g.targets(in.Limits)

	// Lastmass: 16-Pin-Leistung, wenn GPU-Z sie liefert, sonst Board Power (konservativer, enthaelt den Slot-Anteil).
	load := in.PowerW
	if in.Pin16W != nil {
		load = in.Pin16W
	}
	high := load != nil && in.Limits.Cur > 0 && *load >= in.Limits.Cur*g.p.highPct/100
	switch g.state {
	case guardGedrosselt:
		if in.Now.Sub(g.stage2Since).Seconds() >= g.p.recoveryS {
			g.state, g.recoverySince, g.highSince = guardErholung, in.Now, time.Time{}
			ev(guardEvErholung, fmt.Sprintf("Erholung: zurueck auf %.0f W", dauer), dauer)
		}
	case guardErholung:
		if in.Now.Sub(g.recoverySince).Seconds() >= guardErholungMeldenS {
			g.state = guardNormal
		}
		if high { // waehrend der Erholung gleich wieder Vollast: Zaehler laeuft normal weiter
			if g.highSince.IsZero() {
				g.highSince = in.Now
			}
		}
	default:
		if high {
			if g.highSince.IsZero() {
				g.highSince = in.Now
			}
			d := in.Now.Sub(g.highSince).Seconds()
			switch {
			case d >= g.p.highS:
				g.state, g.stage2Since = guardGedrosselt, in.Now
				ev(guardEvGedrosselt, fmt.Sprintf("%.0f s Vollast am Limit - Stufe 2: %.0f W fuer %.0f s", d, stufe2, g.p.recoveryS), stufe2)
			case d >= g.p.highS/2:
				if g.state != guardHochlast {
					ev(guardEvHochlast, fmt.Sprintf("Hochlast seit %.0f s (%.0f W von %.0f W)", d, *load, in.Limits.Cur), 0)
				}
				g.state = guardHochlast
			default:
				g.state = guardNormal
			}
		} else {
			g.highSince = time.Time{}
			g.state = guardNormal
		}
	}
	if !g.highSince.IsZero() {
		st.HighLoadS = int(in.Now.Sub(g.highSince).Seconds())
	}

	// Limit anwenden: gewuenscht = Stufe 2 in gedrosselt, sonst Dauerlimit. Nur senken; tiefer als gewuenscht bleibt.
	want := dauer
	if g.state == guardGedrosselt {
		want = stufe2
	}
	st.TargetW = fptr(want)
	cur := in.Limits.Cur
	// Senken immer; anheben nur von einem Wert, den wir selbst gesetzt haben (Stufe 2 -> Dauerlimit). Ein Fremdtool,
	// das tiefer stellt, bleibt unangetastet - und ueber das Standardlimit geht es nie (targets klemmt).
	ownValue := g.lastSetW > 0 && math.Abs(cur-g.lastSetW) < 0.5
	needSet := cur > want+0.5 || (cur < want-0.5 && ownValue)
	due := g.lastApply.IsZero() || in.Now.Sub(g.lastApply).Seconds() >= g.p.reapplyS || g.lastWant != want
	if needSet && due {
		g.lastApply, g.lastWant = in.Now, want
		if g.setLimit == nil {
			g.denied, g.deniedAt = "kein Setz-Weg (NVML/nvidia-smi)", in.Now
		} else if err := g.setLimit(want); err != nil {
			if g.denied == "" {
				ev(guardEvLimitVerweigert, "Power-Limit nicht setzbar: "+err.Error()+" - nur Beobachtung", want)
			}
			g.denied, g.deniedAt = err.Error(), in.Now
		} else {
			// Fremdeingriff: wir hatten schon auf diesen Wert gesetzt, jemand hat ihn wieder angehoben
			if g.lastSetW == want && cur > want+0.5 {
				g.conflicts++
				if g.conflicts >= 3 && !g.fremdWarned {
					ev(guardEvFremdeingriff, fmt.Sprintf("Limit stand %d-mal wieder auf %.0f W (Fremdeingriff?), erneut %.0f W", g.conflicts, cur, want), want)
					g.fremdWarned = true
				}
			} else {
				g.conflicts = 0
			}
			g.denied, g.lastSetW = "", want
			st.LimitW = fptr(want)
			ev(guardEvLimitGesetzt, fmt.Sprintf("Power-Limit %.0f -> %.0f W gesetzt (%s)", cur, want, g.reasonFor(want, dauer, in.Limits.Def)), want)
		}
	} else if !needSet {
		g.conflicts, g.fremdWarned = 0, false
		if g.denied != "" && in.Now.Sub(g.deniedAt).Seconds() >= g.p.reapplyS {
			g.denied = "" // Limit stimmt inzwischen (jemand anders hat gesetzt): kein Problem mehr
		}
	}
	if g.denied != "" { // der Automat laeuft weiter, nach aussen zaehlt: der Hebel fehlt
		st.State, st.Problem, st.Reason = guardUnverfuegbar, true, "Limit nicht setzbar: "+g.denied
		return st, evs
	}
	st.State = g.state
	if g.conflicts >= 3 {
		st.Warnings = append(st.Warnings, guardEvFremdeingriff)
	}
	st.Problem = len(st.Warnings) > 0
	return st, evs
}

func (g *guardEngine) reasonFor(want, dauer, def float64) string {
	if want < dauer {
		return fmt.Sprintf("Stufe 2, %.0f %% des Standardlimits", g.p.stage2Pct)
	}
	if g.p.limitW > 0 {
		return "Dauerlimit absolut"
	}
	return fmt.Sprintf("Dauerlimit %.0f %% von %.0f W", g.p.limitPct, def)
}

// watchVoltage: Referenz im Leerlauf lernen (< 60 W, eine Probe je Minute, 24 h), unter Last Abfall pruefen.
func (g *guardEngine) watchVoltage(in guardInput, st *GuardStatus, ev func(string, string, float64)) {
	if in.Pin16V == nil || in.Pin16W == nil {
		return
	}
	v, w := *in.Pin16V, *in.Pin16W
	if w < 60 && v > 6 && (g.vrefLast.IsZero() || in.Now.Sub(g.vrefLast) >= time.Minute) {
		g.vref = append(g.vref, v)
		if len(g.vref) > guardVRefMaxProben {
			g.vref = g.vref[1:]
		}
		g.vrefLast = in.Now
	}
	ref := g.vrefMedian()
	st.Pin16RefV = ref
	low := w >= g.p.voltLoadW && (v < g.p.voltWarnV || (ref != nil && *ref-v > g.p.voltDropV))
	if !low {
		g.voltLowSince, g.voltWarned = time.Time{}, false
		return
	}
	if g.voltLowSince.IsZero() {
		g.voltLowSince = in.Now
	}
	if in.Now.Sub(g.voltLowSince).Seconds() < g.p.voltHoldS {
		return
	}
	st.Warnings = append(st.Warnings, guardEvSpannung)
	if !g.voltWarned {
		refS := "keine Referenz"
		if ref != nil {
			refS = fmt.Sprintf("Referenz %.2f V", *ref)
		}
		ev(guardEvSpannung, fmt.Sprintf("16-Pin-Spannung %.2f V bei %.0f W (%s) - Kabel/Stecker pruefen", v, w, refS), 0)
		g.voltWarned = true
	}
}

func (g *guardEngine) watchTemperature(in guardInput, st *GuardStatus, ev func(string, string, float64)) {
	hot := (in.MemTempC != nil && *in.MemTempC >= g.p.memWarnC) || (in.HotspotC != nil && *in.HotspotC >= g.p.hotWarnC)
	if !hot {
		g.tempWarned = false
		return
	}
	st.Warnings = append(st.Warnings, guardEvTemperatur)
	if !g.tempWarned {
		m, h := math.NaN(), math.NaN()
		if in.MemTempC != nil {
			m = *in.MemTempC
		}
		if in.HotspotC != nil {
			h = *in.HotspotC
		}
		ev(guardEvTemperatur, fmt.Sprintf("Speicher %.0f °C, Hot Spot %.0f °C", m, h), 0)
		g.tempWarned = true
	}
}

// watchHardware: hw_slowdown / hw_thermal / hw_power_brake heisst, die Karte bremst selbst - etwas ist ernsthaft falsch.
func (g *guardEngine) watchHardware(in guardInput, st *GuardStatus, ev func(string, string, float64)) {
	hw := false
	for _, r := range in.Throttle {
		if r == "hw_slowdown" || r == "hw_thermal" || r == "hw_power_brake" {
			hw = true
		}
	}
	if !hw {
		g.hwWarned = false
		return
	}
	st.Warnings = append(st.Warnings, guardEvHwDrossel)
	if !g.hwWarned {
		ev(guardEvHwDrossel, fmt.Sprintf("Hardware-Drosselung aktiv: %v", in.Throttle), 0)
		g.hwWarned = true
	}
}

// Guard: Laufzeitmodul um die Engine - tickt alle 2 s mit dem letzten GPU-Sample, haelt Status und Ereignisse fuer
// Heartbeat, /health und MQTT.
type Guard struct {
	cfg    GuardCfg
	gpu    *GPU
	log    *Logger
	engine *guardEngine
	mu     sync.Mutex
	status GuardStatus
	events chan guardEvent
	last   *guardEvent
	count  int
}

func newGuard(cfg GuardCfg, gpu *GPU, log *Logger) *Guard {
	g := &Guard{cfg: cfg, gpu: gpu, log: log, events: make(chan guardEvent, 32)}
	g.engine = newGuardEngine(cfg, gpu.SetPowerLimit)
	g.status = GuardStatus{State: guardUnverfuegbar, Source: "-", Reason: "noch keine Messung"}
	if !cfg.on() {
		g.status = GuardStatus{State: guardAus, Source: "-", Problem: true, Reason: "abgewaehlt"}
	}
	return g
}

// inputFrom baut die Engine-Eingabe aus einem Sample und den Limits der Karte.
func inputFrom(now time.Time, s *GPUSample, lim guardLimits) guardInput {
	in := guardInput{Now: now, Limits: lim}
	if s == nil || s.Sensors == nil {
		return in
	}
	x := s.Sensors
	in.PowerW, in.Pin16W, in.Pin16V, in.MemTempC, in.HotspotC, in.Throttle, in.GPUZ = x.PowerW, x.Pin16PowerW, x.Pin16VoltageV, x.MemTempC, x.HotSpotC, x.ThrottleReasons, x.GPUZ
	return in
}

func (g *Guard) Run(ctx context.Context) {
	p := g.cfg.resolved()
	if !p.enabled {
		g.log.Warnf("gpu-guard: per Konfiguration ABGEWAEHLT (gpu_guard.enabled: false) - kein Power-Limit, Meldung als Problem")
	} else {
		g.log.Infof("gpu-guard: an - Dauerlimit %.0f %% des Standards, Hochlast ab %.0f %% ueber %.0f s -> Stufe 2 %.0f %% fuer %.0f s, Neusetzen alle %.0f s",
			p.limitPct, p.highPct, p.highS, p.stage2Pct, p.recoveryS, p.reapplyS)
	}
	t := time.NewTicker(2 * time.Second)
	defer t.Stop()
	for {
		g.tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func (g *Guard) tick(ctx context.Context) {
	s, _ := g.gpu.Last()
	if s == nil || time.Since(s.At) > 3*time.Second {
		if ns, err := g.gpu.Sample(ctx); err == nil {
			s = ns
		}
	}
	lim := g.gpu.PowerLimits()
	st, evs := g.engine.step(inputFrom(time.Now(), s, lim))
	g.mu.Lock()
	g.status = st
	g.mu.Unlock()
	for _, e := range evs {
		g.emit(e)
	}
}

func (g *Guard) emit(e guardEvent) {
	switch e.Type {
	case guardEvLimitVerweigert, guardEvFremdeingriff, guardEvSpannung, guardEvTemperatur, guardEvHwDrossel, guardEvAbgewaehlt:
		g.log.Warnf("gpu-guard: %s: %s", e.Type, e.Msg)
	default:
		g.log.Infof("gpu-guard: %s: %s", e.Type, e.Msg)
	}
	g.mu.Lock()
	ec := e
	g.last, g.count = &ec, g.count+1
	g.mu.Unlock()
	select {
	case g.events <- e:
	default:
	}
}

// Status: Kopie fuer Heartbeat und /health.
func (g *Guard) Status() GuardStatus {
	if g == nil {
		return GuardStatus{State: guardUnverfuegbar, Source: "-", Reason: "kein Guard"}
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.status
}

// LastEvent: letztes Ereignis und Gesamtzahl (MQTT-Attribute).
func (g *Guard) LastEvent() (*guardEvent, int) {
	if g == nil {
		return nil, 0
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.last, g.count
}
