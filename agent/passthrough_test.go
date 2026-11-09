package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestPassthroughOnlyInferencePaths (ADR-0073, TM-14): every listed path and method reaches the upstream; model
// management, blobs, other methods and near misses are answered 403 without any upstream call.
func TestPassthroughOnlyInferencePaths(t *testing.T) {
	ollama := &countingServer{reply: `{"ok":true}`}
	osrv := httptest.NewServer(ollama)
	defer osrv.Close()
	tun := newTestTunnel(osrv.URL, nil)

	n := 0
	for path, methods := range passthroughAllowed {
		for _, m := range methods {
			st, _, body := respOf(t, runHandle(t, tun, m, path+"?keep=1", `{}`))
			n++
			if st != 200 || body != ollama.reply || len(ollama.calls()) != n {
				t.Fatalf("%s %s: %d %q, upstream calls %d", m, path, st, body, len(ollama.calls()))
			}
		}
	}

	refused := [][2]string{
		{"POST", "/api/pull"}, {"POST", "/api/push"}, {"POST", "/api/create"}, {"POST", "/api/copy"},
		{"DELETE", "/api/delete"}, {"POST", "/api/blobs/sha256:00"}, {"HEAD", "/api/blobs/sha256:00"},
		{"GET", "/api/chat"}, {"DELETE", "/api/tags"}, {"POST", "/api/chat/../pull"}, {"POST", "/API/PULL"},
		{"POST", "/api/pull?name=x"}, {"POST", "/api/chat/"}, {"GET", "/"}, {"POST", "/v1/responses"},
	}
	for _, r := range refused {
		st, _, body := respOf(t, runHandle(t, tun, r[0], r[1], `{"name":"x"}`))
		if st != http.StatusForbidden || !strings.Contains(body, "path_not_allowed_on_node") {
			t.Fatalf("%s %s: %d %q", r[0], r[1], st, body)
		}
	}
	if len(ollama.calls()) != n {
		t.Fatalf("a refused request reached the upstream: %v", ollama.calls()[n:])
	}
}

func TestPassthroughAllowsNoModelManagement(t *testing.T) {
	for _, p := range []string{"/api/pull", "/api/push", "/api/create", "/api/copy", "/api/delete"} {
		if _, ok := passthroughAllowed[p]; ok {
			t.Fatalf("%s is in the passthrough list", p)
		}
	}
	for p := range passthroughAllowed {
		if strings.HasPrefix(p, "/api/blobs") || strings.ContainsAny(p, "?#") || strings.ToLower(p) != p {
			t.Fatalf("unexpected passthrough entry %q", p)
		}
	}
}
