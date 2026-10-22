package main

import (
	"context"
	"fmt"
	"math"
	"sort"
	"sync"
	"time"
)

// GPU guard (gpu_guard, since 0.7.0; draft in design/gpu-guard.md): At 575 W the 12V-2x6 connector of an RTX 5090 carries
// about 48 A over six wires without per-pin measurement. Software cannot detect a bad contact, but can reduce total current
// (Power Limit), cap sustained load, and warn early (voltage drop at the connector, memory temperature).
//
// The core is a pure function (guardEngine.step) over measurements and time - testable without a card. Setting the
// limit goes through the hooked functions limits/setLimit (NVML on Windows, nvidia-smi otherwise). The guard
// only lowers, never raises above the standard limit, and clamps every target to [Min-Limit, Standard-Limit].
//
// States: off | unavailable | normal | high_load | throttled | recovery (English since 0.20.0, design/english-names.md). Disabling (enabled: false) counts as a
// Problem (HA) at the operator's request, so a forgotten disable is noticed.

const (
	guardOff         = "off"
	guardUnavailable = "unavailable"
	guardNormal      = "normal"
	guardHighLoad    = "high_load"
	guardThrottled   = "throttled"
	guardRecovery    = "recovery"

	guardEvLimitSet        = "limit_set"
	guardEvLimitRefused    = "limit_refused"
	guardEvExternalChange  = "external_change"
	guardEvHighLoad        = "high_load"
	guardEvThrottled       = "throttled"
	guardEvRecovery        = "recovery"
	guardEvVoltageLow      = "voltage_low"
	guardEvTemperatureHigh = "temperature_high"
	guardEvHwThrottle      = "hw_throttle"
	guardEvDeselected      = "deselected"

	guardRecoveryReportS = 60.0
	guardVRefMaxSamples  = 1440 // one idle voltage probe per minute, 24 h
)

// GuardCfg: gpu_guard block in the agent configuration. Missing values = defaults (guardDefaults).
type GuardCfg struct {
	Enabled       *bool    `yaml:"enabled" json:"enabled,omitempty"`
	PowerLimitPct *float64 `yaml:"power_limit_pct" json:"power_limit_pct,omitempty"` // Sustained limit as % of the standard limit
	PowerLimitW   *float64 `yaml:"power_limit_w" json:"power_limit_w,omitempty"`     // or absolute; takes precedence over pct
	ReapplyS      *float64 `yaml:"reapply_s" json:"reapply_s,omitempty"`
	HighLoadPct   *float64 `yaml:"high_load_pct" json:"high_load_pct,omitempty"`     // High load above % of the active limit ...
	HighLoadS     *float64 `yaml:"high_load_s" json:"high_load_s,omitempty"`         // ... for this long -> stage 2
	HighLoadGapS  *float64 `yaml:"high_load_gap_s" json:"high_load_gap_s,omitempty"` // Gaps below the threshold up to this duration do not count as interruptions
	Stage2Pct     *float64 `yaml:"stage2_pct" json:"stage2_pct,omitempty"`           // Limit in stage 2 (% of the standard limit)
	RecoveryS     *float64 `yaml:"recovery_s" json:"recovery_s,omitempty"`           // Duration of stage 2
	VoltageWarnV  *float64 `yaml:"voltage_warn_v" json:"voltage_warn_v,omitempty"`
	VoltageDropV  *float64 `yaml:"voltage_drop_v" json:"voltage_drop_v,omitempty"`
	VoltageHoldS  *float64 `yaml:"voltage_hold_s" json:"voltage_hold_s,omitempty"`
	VoltageLoadW  *float64 `yaml:"voltage_load_w" json:"voltage_load_w,omitempty"` // Voltage counts only above this 16-pin power
	MemTempWarnC  *float64 `yaml:"mem_temp_warn_c" json:"mem_temp_warn_c,omitempty"`
	HotspotWarnC  *float64 `yaml:"hotspot_warn_c" json:"hotspot_warn_c,omitempty"`
}

func (c *GuardCfg) on() bool { return c.Enabled == nil || *c.Enabled }

// guardParams: resolved values.
type guardParams struct {
	enabled                                        bool
	limitPct, limitW, reapplyS                     float64
	highPct, highS, highGapS, stage2Pct, recoveryS float64
	voltWarnV, voltDropV, voltHoldS, voltLoadW     float64
	memWarnC, hotWarnC                             float64
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
		highPct: f(c.HighLoadPct, 90), highS: f(c.HighLoadS, 600), highGapS: f(c.HighLoadGapS, 15), stage2Pct: f(c.Stage2Pct, 70), recoveryS: f(c.RecoveryS, 300),
		voltWarnV: f(c.VoltageWarnV, 11.6), voltDropV: f(c.VoltageDropV, 0.35), voltHoldS: f(c.VoltageHoldS, 30), voltLoadW: f(c.VoltageLoadW, 300),
		memWarnC: f(c.MemTempWarnC, 95), hotWarnC: f(c.HotspotWarnC, 100),
	}
}

// guardLimits: what the card reports about its limit (Watts). ok=false = unreadable (no NVML, card without limit).
type guardLimits struct {
	Cur, Def, Min, Max float64
	OK                 bool
	Err                string
}

// guardInput: a measurement.
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

// GuardStatus goes into Heartbeat, /health, and MQTT.
type GuardStatus struct {
	State         string   `json:"state"`
	Source        string   `json:"source"`
	PowerW        *float64 `json:"power_w,omitempty"`
	Pin16W        *float64 `json:"pin16_w,omitempty"`
	Pin16V        *float64 `json:"pin16_v,omitempty"`
	Pin16RefV     *float64 `json:"pin16_ref_v,omitempty"`
	LimitW        *float64 `json:"limit_w,omitempty"`
	DefaultLimitW *float64 `json:"default_limit_w,omitempty"`
	TargetW       *float64 `json:"target_w,omitempty"`
	Throttle      []string `json:"throttle,omitempty"`
	HighLoadS     int      `json:"high_load_s"`
	Problem       bool     `json:"problem"`
	Reason        string   `json:"reason,omitempty"`
	Warnings      []string `json:"warnings,omitempty"`
}

type guardEvent struct {
	Type   string    `json:"event_type"`
	Msg    string    `json:"msg"`
	LimitW float64   `json:"limit_w,omitempty"`
	At     time.Time `json:"at"`
}

// guardEngine holds state between two measurements. Not concurrency-safe - the caller serializes.
type guardEngine struct {
	p        guardParams
	setLimit func(w float64) error // nil = no lever (Tests: Mock)

	state          string
	highSince      time.Time
	lowSince       time.Time // how long load has been below the threshold (gap); empty = currently high
	stage2Since    time.Time
	recoverySince  time.Time
	lastApply      time.Time
	lastWant       float64 // last targeted value (change -> apply immediately, otherwise every reapply_s)
	lastSetW       float64 // what we last successfully set (0 = never)
	conflicts      int
	denied         string
	deniedAt       time.Time
	voltLowSince   time.Time
	voltWarned     bool
	tempWarned     bool
	hwWarned       bool
	externalWarned bool
	vref           []float64 // Leerlauf-Spannungsproben
	vrefLast       time.Time
}

func newGuardEngine(cfg GuardCfg, setLimit func(float64) error) *guardEngine {
	return &guardEngine{p: cfg.resolved(), setLimit: setLimit, state: guardNormal}
}

func clampf(v, lo, hi float64) float64 { return math.Max(lo, math.Min(hi, v)) }

// targets: Sustained limit and stage-2 limit, clamped to [Min, Standard]. Never above standard.
func (g *guardEngine) targets(l guardLimits) (sustained, stage2 float64) {
	lo := l.Min
	if lo <= 0 {
		lo = l.Def * 0.5
	}
	hi := l.Def
	if g.p.limitW > 0 {
		sustained = g.p.limitW
	} else {
		sustained = l.Def * g.p.limitPct / 100
	}
	sustained = clampf(math.Round(sustained), lo, hi)
	stage2 = clampf(math.Round(l.Def*g.p.stage2Pct/100), lo, sustained)
	return sustained, stage2
}

func (g *guardEngine) vrefMedian() *float64 {
	if len(g.vref) < 5 {
		return nil
	}
	c := append([]float64(nil), g.vref...)
	sort.Float64s(c)
	return fptr(math.Round(c[len(c)/2]*1000) / 1000)
}

// step evaluates a measurement, sets the limit if needed (via setLimit), and returns status and events.
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

	// Warnings that apply regardless of the limit (also when off/unavailable): Voltage, Temperature, Hardware throttling.
	g.watchVoltage(in, &st, ev)
	g.watchTemperature(in, &st, ev)
	g.watchHardware(in, &st, ev)

	if !g.p.enabled {
		if g.state != guardOff {
			ev(guardEvDeselected, "GPU-Schutz per Konfiguration abgewaehlt - kein Limit, nur Messwerte", 0)
		}
		g.state = guardOff
		st.State, st.Problem, st.Reason = guardOff, true, guardEvDeselected
		return st, evs
	}
	if !in.Limits.OK || in.Limits.Def <= 0 {
		g.state = guardUnavailable
		st.State, st.Problem, st.Reason = guardUnavailable, true, firstNonEmpty(in.Limits.Err, "Power-Limit nicht lesbar")
		return st, evs
	}
	sustained, stage2 := g.targets(in.Limits)

	// Load metric: Board Power from NVML. It includes the slot share, so it is never below the 16-pin power, and the
	// limit applies to it. The 16-pin values from GPU-Z come via the relay (POST /gpuz, any local process could
	// send them) - they remain for display and warning, but do not decide the limit (Review 2026-09-25).
	load := in.PowerW
	// High load: Power >= highPct of the active limit. Between two requests, power drops for milliseconds
	// (queue, response); measured 2026-09-25: with 2-s sampling, the counter never exceeded
	// a few seconds under sustained batch load. Therefore, a gap counts as an interruption only after highGapS below the threshold.
	high := load != nil && in.Limits.Cur > 0 && *load >= in.Limits.Cur*g.p.highPct/100
	if high {
		g.lowSince = time.Time{}
	} else if !g.highSince.IsZero() {
		if g.lowSince.IsZero() {
			g.lowSince = in.Now
		}
		if in.Now.Sub(g.lowSince).Seconds() < g.p.highGapS {
			high = true // short gap: counter continues
		}
	}
	switch g.state {
	case guardThrottled:
		if in.Now.Sub(g.stage2Since).Seconds() >= g.p.recoveryS {
			g.state, g.recoverySince, g.highSince = guardRecovery, in.Now, time.Time{}
			ev(guardEvRecovery, fmt.Sprintf("Erholung: zurueck auf %.0f W", sustained), sustained)
		}
	case guardRecovery:
		if in.Now.Sub(g.recoverySince).Seconds() >= guardRecoveryReportS {
			g.state = guardNormal
		}
		if high { // full load again during recovery: counter continues normally
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
				g.state, g.stage2Since = guardThrottled, in.Now
				ev(guardEvThrottled, fmt.Sprintf("%.0f s Vollast am Limit - Stufe 2: %.0f W fuer %.0f s", d, stage2, g.p.recoveryS), stage2)
			case d >= g.p.highS/2:
				if g.state != guardHighLoad {
					ev(guardEvHighLoad, fmt.Sprintf("Hochlast seit %.0f s (%.0f W von %.0f W)", d, *load, in.Limits.Cur), 0)
				}
				g.state = guardHighLoad
			default:
				g.state = guardNormal
			}
		} else {
			g.highSince, g.lowSince = time.Time{}, time.Time{}
			g.state = guardNormal
		}
	}
	if !g.highSince.IsZero() {
		st.HighLoadS = int(in.Now.Sub(g.highSince).Seconds())
	}

	// Apply limit: desired = stage 2 if throttled, else sustained limit. Only lower; stays lower if desired is higher.
	want := sustained
	if g.state == guardThrottled {
		want = stage2
	}
	st.TargetW = fptr(want)
	cur := in.Limits.Cur
	// Lower always; raise only from a value we set ourselves (stage 2 -> sustained limit). An external tool,
	// set deeper, remains untouched - and it never exceeds the default limit (targets clamp).
	// "Self-set" means: the limit is at one of our target values. lastSetW lives only in memory; after
	// a service restart mid-stage 2, the card would otherwise stay at 70 % forever (Review 2026-09-25).
	ownValue := (g.lastSetW > 0 && math.Abs(cur-g.lastSetW) < 0.5) || math.Abs(cur-stage2) < 0.5 || math.Abs(cur-sustained) < 0.5
	needSet := cur > want+0.5 || (cur < want-0.5 && ownValue)
	due := g.lastApply.IsZero() || in.Now.Sub(g.lastApply).Seconds() >= g.p.reapplyS || g.lastWant != want
	if needSet && due {
		g.lastApply, g.lastWant = in.Now, want
		if g.setLimit == nil {
			g.denied, g.deniedAt = "kein Setz-Weg (NVML/nvidia-smi)", in.Now
		} else if err := g.setLimit(want); err != nil {
			if g.denied == "" {
				ev(guardEvLimitRefused, "Power-Limit nicht setzbar: "+err.Error()+" - nur Beobachtung", want)
			}
			g.denied, g.deniedAt = err.Error(), in.Now
		} else {
			// External interference: we had already set this value, someone raised it again
			if g.lastSetW == want && cur > want+0.5 {
				g.conflicts++
				if g.conflicts >= 3 && !g.externalWarned {
					ev(guardEvExternalChange, fmt.Sprintf("Limit stand %d-mal wieder auf %.0f W (Fremdeingriff?), erneut %.0f W", g.conflicts, cur, want), want)
					g.externalWarned = true
				}
			} else {
				g.conflicts = 0
			}
			g.denied, g.lastSetW = "", want
			st.LimitW = fptr(want)
			ev(guardEvLimitSet, fmt.Sprintf("Power-Limit %.0f -> %.0f W gesetzt (%s)", cur, want, g.reasonFor(want, sustained, in.Limits.Def)), want)
		}
	} else if !needSet {
		g.conflicts, g.externalWarned = 0, false
		if g.denied != "" && in.Now.Sub(g.deniedAt).Seconds() >= g.p.reapplyS {
			g.denied = "" // Limit is now correct (someone else set it): no problem anymore
		}
	}
	if g.denied != "" { // the automaton continues, externally counts: the lever is missing
		st.State, st.Problem, st.Reason = guardUnavailable, true, "Limit nicht setzbar: "+g.denied
		return st, evs
	}
	st.State = g.state
	if g.conflicts >= 3 {
		st.Warnings = append(st.Warnings, guardEvExternalChange)
	}
	st.Problem = len(st.Warnings) > 0
	return st, evs
}

func (g *guardEngine) reasonFor(want, sustained, def float64) string {
	if want < sustained {
		return fmt.Sprintf("Stufe 2, %.0f %% des Standardlimits", g.p.stage2Pct)
	}
	if g.p.limitW > 0 {
		return "Dauerlimit absolut"
	}
	return fmt.Sprintf("Dauerlimit %.0f %% von %.0f W", g.p.limitPct, def)
}

// watchVoltage: learn idle reference (< 60 W, one sample per minute, 24 h), check drop under load.
func (g *guardEngine) watchVoltage(in guardInput, st *GuardStatus, ev func(string, string, float64)) {
	if in.Pin16V == nil || in.Pin16W == nil {
		return
	}
	v, w := *in.Pin16V, *in.Pin16W
	if w < 60 && v > 6 && (g.vrefLast.IsZero() || in.Now.Sub(g.vrefLast) >= time.Minute) {
		g.vref = append(g.vref, v)
		if len(g.vref) > guardVRefMaxSamples {
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
	st.Warnings = append(st.Warnings, guardEvVoltageLow)
	if !g.voltWarned {
		refS := "keine Referenz"
		if ref != nil {
			refS = fmt.Sprintf("Referenz %.2f V", *ref)
		}
		ev(guardEvVoltageLow, fmt.Sprintf("16-Pin-Spannung %.2f V bei %.0f W (%s) - Kabel/Stecker pruefen", v, w, refS), 0)
		g.voltWarned = true
	}
}

func (g *guardEngine) watchTemperature(in guardInput, st *GuardStatus, ev func(string, string, float64)) {
	hot := (in.MemTempC != nil && *in.MemTempC >= g.p.memWarnC) || (in.HotspotC != nil && *in.HotspotC >= g.p.hotWarnC)
	if !hot {
		g.tempWarned = false
		return
	}
	st.Warnings = append(st.Warnings, guardEvTemperatureHigh)
	if !g.tempWarned {
		m, h := math.NaN(), math.NaN()
		if in.MemTempC != nil {
			m = *in.MemTempC
		}
		if in.HotspotC != nil {
			h = *in.HotspotC
		}
		ev(guardEvTemperatureHigh, fmt.Sprintf("Speicher %.0f °C, Hot Spot %.0f °C", m, h), 0)
		g.tempWarned = true
	}
}

// watchHardware: hw_slowdown / hw_thermal / hw_power_brake means the card is throttling itself - something is seriously wrong.
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
	st.Warnings = append(st.Warnings, guardEvHwThrottle)
	if !g.hwWarned {
		ev(guardEvHwThrottle, fmt.Sprintf("Hardware-Drosselung aktiv: %v", in.Throttle), 0)
		g.hwWarned = true
	}
}

// Guard: runtime module around the engine - ticks every 2 s with the last GPU sample, holds status and events for
// Heartbeat, /health and MQTT.
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
	g.status = GuardStatus{State: guardUnavailable, Source: "-", Reason: "noch keine Messung"}
	if !cfg.on() {
		g.status = GuardStatus{State: guardOff, Source: "-", Problem: true, Reason: guardEvDeselected}
	}
	return g
}

// inputFrom builds the engine input from a sample and the card's limits.
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
		g.log.Warnf("gpu-guard: DESELECTED by configuration (gpu_guard.enabled: false) - no power limit, reported as a problem")
	} else {
		g.log.Infof("gpu-guard: on - sustained limit %.0f %% of default, high load from %.0f %% over %.0f s -> stage 2 %.0f %% for %.0f s, reapply every %.0f s",
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
	case guardEvLimitRefused, guardEvExternalChange, guardEvVoltageLow, guardEvTemperatureHigh, guardEvHwThrottle, guardEvDeselected:
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

// Status: copy for Heartbeat and /health.
func (g *Guard) Status() GuardStatus {
	if g == nil {
		return GuardStatus{State: guardUnavailable, Source: "-", Reason: "kein Guard"}
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.status
}

// LastEvent: last event and total count (MQTT attributes).
func (g *Guard) LastEvent() (*guardEvent, int) {
	if g == nil {
		return nil, 0
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.last, g.count
}
