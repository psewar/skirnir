package main

import (
	"encoding/json"
	"time"

	"github.com/gorilla/websocket"
)

// Sleep and wake-up as a fact for the router (frame POWER, 0.24.0). Before, the router only noticed a sleeping
// machine by a ping timeout about a minute later, and short standby wake-ups looked like a flapping tunnel
// (a desktop node, 2026-10-23). The Windows service gets the events from the SCM (service_windows.go); Linux and
// console mode report nothing, and the router then keeps power = unknown.

const (
	powerSuspending = "suspending"
	powerResumed    = "resumed"
	// The system suspends shortly after the notice; the frame gets this long, then the agent lets the machine sleep.
	powerSendWithin = 1500 * time.Millisecond
)

// PowerEvent is what the agent reports. Via names the source, e.g. the Windows broadcast.
type PowerEvent struct {
	State string    `json:"state"`
	At    time.Time `json:"at"`
	Via   string    `json:"via"`
}

// ReportPower sends the event to the router at once and remembers it, so a new tunnel session repeats the latest
// state after a reconnect. Returns whether the frame went out; without a tunnel it stays remembered only.
func (t *Tunnel) ReportPower(ev PowerEvent) bool {
	t.mu.Lock()
	t.lastPower = &ev
	conn := t.conn
	t.mu.Unlock()
	if conn == nil {
		return false
	}
	return t.sendPower(conn, ev) == nil
}

func (t *Tunnel) sendPower(conn *websocket.Conn, ev PowerEvent) error {
	b, _ := json.Marshal(ev)
	return t.writeWithin(conn, tunPower, 0, b, powerSendWithin)
}

// replayPower repeats the latest power event at the start of a session (the resume notice often comes before the
// network is back, so it is sent with the first connection after it).
func (t *Tunnel) replayPower(conn *websocket.Conn) {
	t.mu.Lock()
	ev := t.lastPower
	t.mu.Unlock()
	if ev != nil {
		_ = t.sendPower(conn, *ev)
	}
}

// Power records a power event from the service and passes it to the tunnel; the same state twice in a row (Windows
// sends PBT_APMRESUMEAUTOMATIC and, on user input, PBT_APMRESUMESUSPEND) is reported once.
func (a *App) Power(state, via string) {
	a.powerMu.Lock()
	same := a.powerState == state
	a.powerState = state
	a.powerMu.Unlock()
	if same {
		return
	}
	sent := false
	if a.tunnel != nil {
		sent = a.tunnel.ReportPower(PowerEvent{State: state, At: time.Now().UTC(), Via: via})
	}
	a.log.Infof("power: %s (%s), reported to the router: %v", state, via, sent)
}
