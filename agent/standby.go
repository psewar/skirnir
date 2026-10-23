package main

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Models of the standby Ollama (tunnel.upstream_fallback) as a fact from its model store, also while it is stopped.
// The router (0.15.0) may hold the OpenAI-compatible server for a request that needs one of them and let the standby
// serve; without this list it only knew the standby's models after a game had once started it. Read from
// <OLLAMA_MODELS>/manifests/<host>/<namespace>/<model>/<tag>, the layout Ollama itself writes.

const standbyRefresh = 60 * time.Second

type StandbyStore struct {
	dir string

	mu     sync.Mutex
	models []string
	at     time.Time
}

// newStandbyStore: nil without tunnel.upstream_fallback. The directory comes from OLLAMA_MODELS in the standby
// child's env, then from the agent's own environment, then Ollama's default under the user profile.
func newStandbyStore(cfg *Config) *StandbyStore {
	fb := cfg.Tunnel.UpstreamFallback
	if fb == nil || fb.Child == "" {
		return nil
	}
	dir := ""
	for _, ch := range cfg.Children {
		if ch.Name != fb.Child {
			continue
		}
		for _, kv := range ch.Env {
			if v, ok := strings.CutPrefix(kv, "OLLAMA_MODELS="); ok {
				dir = strings.Trim(v, `"'`)
			}
		}
	}
	if dir == "" {
		dir = os.Getenv("OLLAMA_MODELS")
	}
	if dir == "" {
		if home, err := os.UserHomeDir(); err == nil {
			dir = filepath.Join(home, ".ollama", "models")
		}
	}
	return &StandbyStore{dir: dir}
}

// Models returns the store's model names (refreshed at most every standbyRefresh); empty when the store is missing.
func (s *StandbyStore) Models() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.models != nil && time.Since(s.at) < standbyRefresh {
		return s.models
	}
	s.models, s.at = listManifests(filepath.Join(s.dir, "manifests")), time.Now()
	return s.models
}

// listManifests turns <root>/<host>/<namespace>/<model>/<tag> into Ollama's names: "model:tag" for the default
// registry's library, "namespace/model:tag" for its other namespaces, "host/namespace/model:tag" otherwise.
func listManifests(root string) []string {
	out := []string{}
	if st, err := os.Stat(root); err != nil || !st.IsDir() {
		return out
	}
	hosts, _ := os.ReadDir(root)
	for _, h := range hosts {
		if !h.IsDir() {
			continue
		}
		spaces, _ := os.ReadDir(filepath.Join(root, h.Name()))
		for _, ns := range spaces {
			if !ns.IsDir() {
				continue
			}
			repos, _ := os.ReadDir(filepath.Join(root, h.Name(), ns.Name()))
			for _, repo := range repos {
				if !repo.IsDir() {
					continue
				}
				tags, _ := os.ReadDir(filepath.Join(root, h.Name(), ns.Name(), repo.Name()))
				for _, tag := range tags {
					if tag.IsDir() {
						continue
					}
					name := repo.Name() + ":" + tag.Name()
					switch {
					case h.Name() == "registry.ollama.ai" && ns.Name() == "library":
					case h.Name() == "registry.ollama.ai":
						name = ns.Name() + "/" + name
					default:
						name = h.Name() + "/" + ns.Name() + "/" + name
					}
					out = append(out, name)
				}
			}
		}
	}
	sort.Strings(out)
	return out
}
