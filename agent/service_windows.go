//go:build windows

package main

import (
	"context"
	"fmt"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
)

// Power broadcast event types (SERVICE_CONTROL_POWEREVENT, winuser.h).
const (
	pbtAPMSuspend         = 0x0004
	pbtAPMResumeSuspend   = 0x0007
	pbtAPMResumeAutomatic = 0x0012
)

// windowsService binds the app to the Service Control Manager.
type windowsService struct {
	cfgPath string
	name    string
}

func (ws *windowsService) Execute(args []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	elog, _ := eventlog.Open(ws.name)
	ev := func(f func(uint32, string) error, msg string) {
		if elog != nil && f != nil {
			_ = f(1, msg)
		}
	}
	cfg, err := loadConfig(ws.cfgPath)
	if err != nil {
		ev(elog.Error, fmt.Sprintf("configuration: %v", err))
		return true, 1
	}
	log, err := newLogger(cfg.Logging, "agent", false)
	if err != nil {
		ev(elog.Error, fmt.Sprintf("Logger: %v", err))
		return true, 2
	}
	app, err := newApp(cfg, log)
	if err != nil {
		log.Errorf("Start: %v", err)
		ev(elog.Error, fmt.Sprintf("Start: %v", err))
		return true, 3
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- app.Run(ctx) }()
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown | svc.AcceptPowerEvent}
	ev(elog.Info, fmt.Sprintf("skirnir-agent %s started (node %s)", version, cfg.Node))

	for {
		select {
		case c := <-req:
			switch c.Cmd {
			case svc.Interrogate:
				status <- c.CurrentStatus
			case svc.PowerEvent:
				// Sleep/wake-up as a fact for the router (power.go). The suspend notice is sent here, synchronously and
				// bounded by powerSendWithin, so it leaves before the machine sleeps; a resume goes on in the background.
				switch c.EventType {
				case pbtAPMSuspend:
					app.Power(powerSuspending, "PBT_APMSUSPEND")
				case pbtAPMResumeAutomatic:
					go app.Power(powerResumed, "PBT_APMRESUMEAUTOMATIC")
				case pbtAPMResumeSuspend:
					go app.Power(powerResumed, "PBT_APMRESUMESUSPEND")
				}
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending, WaitHint: 25000}
				cancel()
				select {
				case err := <-done:
					if err != nil {
						log.Errorf("stop: %v", err)
					}
				case <-time.After(25 * time.Second):
					log.Errorf("stop: timeout")
				}
				ev(elog.Info, "skirnir-agent gestoppt")
				return false, 0
			}
		case err := <-done:
			// App exited on its own (should not happen): report as an error so recovery kicks in
			log.Errorf("app stopped: %v", err)
			ev(elog.Error, fmt.Sprintf("app stopped unexpectedly: %v", err))
			return true, 4
		}
	}
}
