package main

import (
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"time"

	"gopkg.in/natefinch/lumberjack.v2"
)

// Logger schreibt in eine rotierte Datei und optional auf die Konsole (run-Modus).
type Logger struct {
	*log.Logger
	debug bool
	file  *lumberjack.Logger
}

func newLogger(cfg LogCfg, name string, console bool) (*Logger, error) {
	if err := os.MkdirAll(cfg.Dir, 0o755); err != nil {
		return nil, fmt.Errorf("Log-Verzeichnis %s: %w", cfg.Dir, err)
	}
	lj := &lumberjack.Logger{
		Filename:   filepath.Join(cfg.Dir, name+".log"),
		MaxSize:    cfg.MaxSizeMB,
		MaxBackups: cfg.MaxBackups,
		LocalTime:  true,
	}
	var w io.Writer = lj
	if console {
		w = io.MultiWriter(os.Stdout, lj)
	}
	return &Logger{Logger: log.New(w, "", 0), debug: cfg.Debug, file: lj}, nil
}

func (l *Logger) line(level, format string, a ...any) {
	l.Logger.Printf("%s %-5s %s", time.Now().Format("2006-01-02 15:04:05"), level, fmt.Sprintf(format, a...))
}

func (l *Logger) Infof(format string, a ...any)  { l.line("INFO", format, a...) }
func (l *Logger) Warnf(format string, a ...any)  { l.line("WARN", format, a...) }
func (l *Logger) Errorf(format string, a ...any) { l.line("ERROR", format, a...) }
func (l *Logger) Debugf(format string, a ...any) {
	if l.debug {
		l.line("DEBUG", format, a...)
	}
}

// childWriter ist das Ziel fuer stdout/stderr eines Kindprozesses: eigene rotierte Datei,
// jede Zeile mit Zeitstempel, damit man Absturzzeitpunkte findet.
type childWriter struct {
	lj  *lumberjack.Logger
	buf []byte
}

func newChildWriter(cfg LogCfg, name string) *childWriter {
	return &childWriter{lj: &lumberjack.Logger{
		Filename: filepath.Join(cfg.Dir, name+".log"), MaxSize: cfg.MaxSizeMB, MaxBackups: cfg.MaxBackups, LocalTime: true}}
}

func (w *childWriter) Write(p []byte) (int, error) {
	w.buf = append(w.buf, p...)
	for {
		i := indexByte(w.buf, '\n')
		if i < 0 {
			break
		}
		line := w.buf[:i]
		if len(line) > 0 && line[len(line)-1] == '\r' {
			line = line[:len(line)-1]
		}
		fmt.Fprintf(w.lj, "%s %s\n", time.Now().Format("2006-01-02 15:04:05"), line)
		w.buf = w.buf[i+1:]
	}
	if len(w.buf) > 64*1024 { // eine endlose Zeile nicht im Speicher sammeln
		fmt.Fprintf(w.lj, "%s %s\n", time.Now().Format("2006-01-02 15:04:05"), w.buf)
		w.buf = w.buf[:0]
	}
	return len(p), nil
}

func indexByte(b []byte, c byte) int {
	for i, x := range b {
		if x == c {
			return i
		}
	}
	return -1
}
