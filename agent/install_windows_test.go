//go:build windows

package main

import "testing"

// Agent 0.36.0: install checks whether the event log source exists instead of matching the error text of a failed
// creation. A source that is not registered is an answer (false), not an error.
func TestEventSourceExistsMissing(t *testing.T) {
	exists, err := eventSourceExists("SkirnirAgentTestSourceThatDoesNotExist")
	if err != nil || exists {
		t.Fatalf("missing source: exists=%v err=%v, want false without error", exists, err)
	}
}
