//go:build windows

package main

import (
	"context"
	"fmt"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/eventlog"
)

// windowsService bindet App an den Service Control Manager.
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
		ev(elog.Error, fmt.Sprintf("Konfiguration: %v", err))
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
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	ev(elog.Info, fmt.Sprintf("ollama-router-agent %s gestartet (node %s)", version, cfg.Node))

	for {
		select {
		case c := <-req:
			switch c.Cmd {
			case svc.Interrogate:
				status <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending, WaitHint: 25000}
				cancel()
				select {
				case err := <-done:
					if err != nil {
						log.Errorf("Stopp: %v", err)
					}
				case <-time.After(25 * time.Second):
					log.Errorf("Stopp: Timeout")
				}
				ev(elog.Info, "ollama-router-agent gestoppt")
				return false, 0
			}
		case err := <-done:
			// App ist von selbst zu Ende (sollte nicht passieren): als Fehler melden, damit Recovery greift
			log.Errorf("App beendet: %v", err)
			ev(elog.Error, fmt.Sprintf("App unerwartet beendet: %v", err))
			return true, 4
		}
	}
}
