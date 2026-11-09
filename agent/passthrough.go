package main

import (
	"encoding/json"
	"net/http"
	"strings"

	"github.com/gorilla/websocket"
)

// passthroughAllowed: the paths and methods the router sends through the tunnel to the node's upstream (Ollama or an
// OpenAI-compatible server) - inference, model listing and versions (ADR-0073, TM-14). Everything else, above all model
// management (/api/pull, /api/push, /api/create, /api/copy, /api/delete), blob uploads (/api/blobs/...) and paths a later
// Ollama adds, is answered 403 by the agent without reaching the upstream. The router already refuses these paths for its
// clients (FORBIDDEN_PATHS); this is the node's own check, so a compromised router cannot use them against an Ollama that
// runs with the agent's service rights. Configured routes (routes:) are checked before and are not affected.
var passthroughAllowed = map[string][]string{
	"/api/chat":            {http.MethodPost},
	"/api/generate":        {http.MethodPost},
	"/api/embed":           {http.MethodPost},
	"/api/embeddings":      {http.MethodPost},
	"/api/show":            {http.MethodPost},
	"/api/tags":            {http.MethodGet},
	"/api/ps":              {http.MethodGet},
	"/api/version":         {http.MethodGet},
	"/v1/chat/completions": {http.MethodPost},
	"/v1/embeddings":       {http.MethodPost},
	"/v1/models":           {http.MethodGet},
}

// passthroughAllows compares the path without query exactly (no prefix, no case folding, no cleaning), so "/api/chat/../pull"
// or "/API/PULL" are refused as well.
func passthroughAllows(method, path string) bool {
	if i := strings.IndexAny(path, "?#"); i >= 0 {
		path = path[:i]
	}
	for _, m := range passthroughAllowed[path] {
		if m == method {
			return true
		}
	}
	return false
}

// pathNotAllowed answers a refused passthrough request with 403 and the reason, like methodNotAllowed for routes.
func (t *Tunnel) pathNotAllowed(conn *websocket.Conn, sid uint32, method, path string) {
	if i := strings.IndexAny(path, "?#"); i >= 0 {
		path = path[:i]
	}
	t.log.Warnf("tunnel: %s %s refused - not an inference path the agent forwards (ADR-0073)", method, path)
	hb, _ := json.Marshal(map[string]any{"status": http.StatusForbidden, "headers": map[string]string{"Content-Type": "application/json"}})
	if t.write(conn, tunResp, sid, hb) != nil {
		return
	}
	b, _ := json.Marshal(map[string]any{"error": map[string]string{"reason": "path_not_allowed_on_node"}})
	if t.write(conn, tunData, sid, b) != nil {
		return
	}
	_ = t.write(conn, tunEnd, sid, nil)
}
