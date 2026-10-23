package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// powerPair: a tunnel whose connection ends at a test server that returns every frame it receives.
func powerPair(t *testing.T) (*Tunnel, *websocket.Conn, <-chan []byte) {
	t.Helper()
	frames := make(chan []byte, 8)
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		for {
			_, data, err := c.ReadMessage()
			if err != nil {
				return
			}
			frames <- data
		}
	}))
	t.Cleanup(srv.Close)
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	log, err := newLogger(LogCfg{Dir: t.TempDir()}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() }) // Windows does not delete open files
	tun := newTunnel("https://router.example.net", "http://127.0.0.1:11434", nil, nil, nil, log)
	return tun, conn, frames
}

func readPower(t *testing.T, frames <-chan []byte) PowerEvent {
	t.Helper()
	select {
	case f := <-frames:
		if f[0] != tunPower {
			t.Fatalf("frame type %d, want POWER (%d)", f[0], tunPower)
		}
		var ev PowerEvent
		if err := json.Unmarshal(f[5:], &ev); err != nil {
			t.Fatal(err)
		}
		return ev
	case <-time.After(3 * time.Second):
		t.Fatal("no POWER frame")
	}
	return PowerEvent{}
}

func TestPowerReportGoesOutAtOnceAndIsReplayed(t *testing.T) {
	tun, conn, frames := powerPair(t)
	// without a connection the event is only remembered
	if tun.ReportPower(PowerEvent{State: powerSuspending, At: time.Now(), Via: "test"}) {
		t.Fatal("reported as sent without a tunnel")
	}
	tun.mu.Lock()
	tun.conn = conn
	tun.mu.Unlock()
	if !tun.ReportPower(PowerEvent{State: powerResumed, At: time.Now(), Via: "PBT_APMRESUMEAUTOMATIC"}) {
		t.Fatal("resume notice not sent")
	}
	if ev := readPower(t, frames); ev.State != powerResumed || ev.Via != "PBT_APMRESUMEAUTOMATIC" {
		t.Fatalf("got %+v", ev)
	}
	// a new session repeats the latest state, so the router learns it even if the notice came before the network
	tun.replayPower(conn)
	if ev := readPower(t, frames); ev.State != powerResumed {
		t.Fatalf("replay %+v", ev)
	}
}

func TestAppPowerReportsEachChangeOnce(t *testing.T) {
	tun, conn, frames := powerPair(t)
	tun.mu.Lock()
	tun.conn = conn
	tun.mu.Unlock()
	a := &App{tunnel: tun, log: tun.log}
	a.Power(powerSuspending, "PBT_APMSUSPEND")
	a.Power(powerResumed, "PBT_APMRESUMEAUTOMATIC")
	a.Power(powerResumed, "PBT_APMRESUMESUSPEND") // Windows sends both on a wake-up by the user
	if ev := readPower(t, frames); ev.State != powerSuspending {
		t.Fatalf("first %+v", ev)
	}
	if ev := readPower(t, frames); ev.State != powerResumed {
		t.Fatalf("second %+v", ev)
	}
	select {
	case f := <-frames:
		t.Fatalf("duplicate resume sent: %q", f)
	case <-time.After(300 * time.Millisecond):
	}
}

func TestAppPowerWithoutTunnelOnlyLogs(t *testing.T) {
	log, err := newLogger(LogCfg{Dir: t.TempDir()}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() }) // Windows does not delete open files
	a := &App{log: log}                    // no router configured: the event is logged, nothing else happens
	a.Power(powerSuspending, "PBT_APMSUSPEND")
	if a.powerState != powerSuspending {
		t.Fatalf("state %q", a.powerState)
	}
}
