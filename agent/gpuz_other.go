//go:build !windows

package main

import "errors"

// GPU-Z gibt es nur unter Windows; hier bleiben Leser und Relay leer.
type gpuzReader struct{}

func newGPUZReader(*Logger) *gpuzReader { return nil }

func (r *gpuzReader) read() (*gpuzData, error) { return nil, errors.New("GPU-Z only on Windows") }

func runGPUZRelay(string) error { return errors.New("gpuz-relay exists only on Windows") }
