package main

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

// What the agent remembers about its OpenAI-compatible upstream (agent 0.26.0), in <identity dir>/upstream.json:
//   - its models, as the watcher saw them while the server answered. With them the agent can start with the upstream
//     held and still list its models, so the router can release it for a request without the server ever running.
//   - its RAM need. Before a release the agent checks it against the free RAM (upstream_hold.go ramBlock). Since agent
//     0.35.0 (ADR-0092) the need is the larger of the peak resident memory (working set) of the server's process tree and
//     the RAM that became available when the server stopped, plus needMarginPercent; it only grows while the server's
//     start configuration stays the same. Agents 0.26.0 to 0.34.x stored the peak private bytes (commit) of the last run,
//     which overstated the need; such a value (measure "") is discarded and learned again.
//
// Without the file (first start after the update, or a new upstream) the agent starts the upstream once as before and
// learns both. Trigger 2026-10-24: after every restart the agent started Strata (~38 GB into RAM) although the router
// keeps it held outside its window; one such start at 98 % RAM ended in a bluescreen.
type upstreamMemo struct {
	Tags      []json.RawMessage `json:"tags"`
	NeedBytes uint64            `json:"need_bytes"` // what the RAM check uses; 0 = not learned yet, no check
	// Since 0.35.0: how the need was measured and what it comes from
	Measure           string    `json:"measure,omitempty"`             // needMeasure; "" = commit of an older agent
	Config            string    `json:"config,omitempty"`              // Supervisor.childConfig of the upstream when learned
	PeakResidentBytes uint64    `json:"peak_resident_bytes,omitempty"` // highest working set of the tree, sampled
	ReleasedBytes     uint64    `json:"released_bytes,omitempty"`      // most RAM that became available at a stop
	PeakCommitBytes   uint64    `json:"peak_commit_bytes,omitempty"`   // highest commit, for comparison only
	Updated           time.Time `json:"updated"`
}

const upstreamMemoFile = "upstream.json"

// needMeasure names the measure of NeedBytes (ADR-0092). A memo with another measure has its need learned again.
const needMeasure = "resident"

// needMarginPercent: added to the measured peak. The sampler looks every 30 s and misses short peaks (loading), and the
// RAM released at a stop shows the end of a run, not its top.
const needMarginPercent = 10

// known: enough to start held - the router needs the models to release the upstream for a request.
func (m upstreamMemo) known() bool { return len(m.Tags) > 0 }

// need: the RAM need from the measured peaks - the larger of resident and released, plus the margin. 0 = nothing measured.
func (m upstreamMemo) need() uint64 {
	p := max(m.PeakResidentBytes, m.ReleasedBytes)
	return p + p*needMarginPercent/100
}

// forget drops what was learned about the RAM need (not the models): the next run learns it again.
func (m *upstreamMemo) forget(config string) {
	m.NeedBytes, m.PeakResidentBytes, m.ReleasedBytes, m.PeakCommitBytes = 0, 0, 0, 0
	m.Measure, m.Config = needMeasure, config
}

// loadUpstreamMemo: the remembered state, or an empty memo if there is none yet. An unreadable or broken file is
// reported as an error; the agent then behaves as without one (starts the upstream and learns again).
func loadUpstreamMemo(path string) (upstreamMemo, error) {
	var m upstreamMemo
	b, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) { // the normal first start; checking first would race with the write anyway
		return m, nil
	}
	if err != nil {
		return m, err
	}
	if err := json.Unmarshal(b, &m); err != nil {
		return upstreamMemo{}, err
	}
	return m, nil
}

// saveUpstreamMemo writes atomically (temp file + rename): a crash mid-write must not leave a half file that would make
// the next start learn again or, worse, start held without models.
func saveUpstreamMemo(path string, m upstreamMemo) error {
	// Compact, not indented: the tags must stay byte-equal to the watcher's, or every sample would count as a change
	b, err := json.Marshal(m)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
