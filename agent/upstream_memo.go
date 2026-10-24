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
//   - its RAM need, the peak private bytes of the server's process tree during its last run. Before a release the agent
//     checks that need against the free RAM (upstream_hold.go ramBlock).
//
// Without the file (first start after the update, or a new upstream) the agent starts the upstream once as before and
// learns both. Trigger 2026-10-24: after every restart the agent started Strata (~38 GB into RAM) although the router
// keeps it held outside its window; one such start at 98 % RAM ended in a bluescreen.
type upstreamMemo struct {
	Tags      []json.RawMessage `json:"tags"`
	NeedBytes uint64            `json:"need_bytes"`
	Updated   time.Time         `json:"updated"`
}

const upstreamMemoFile = "upstream.json"

// known: enough to start held - the router needs the models to release the upstream for a request.
func (m upstreamMemo) known() bool { return len(m.Tags) > 0 }

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
