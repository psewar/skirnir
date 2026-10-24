//go:build windows

package main

import (
	"os"
	"testing"
)

// The real call: the test process has committed private memory; a wrong struct size or layout would return false or 0.
func TestProcPrivateBytes(t *testing.T) {
	b, ok := procPrivateBytes(uint32(os.Getpid()))
	if !ok || b < 1<<20 {
		t.Fatalf("own process: %d bytes, ok=%v", b, ok)
	}
	big := make([]byte, 256<<20)
	for i := range big {
		big[i] = 1
	}
	after, ok := procPrivateBytes(uint32(os.Getpid()))
	if !ok || after < b+200<<20 {
		t.Fatalf("256 MiB more committed: %d -> %d bytes", b, after)
	}
	_ = big[len(big)-1]
	if _, ok := procPrivateBytes(0xFFFFFFF0); ok {
		t.Fatal("a process that does not exist: false")
	}
}
