package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"strings"
	"time"
)

// Facts: what the agent knows about its machine and reports to the router during connection setup.
// The router creates the node (Name, GPU, VRAM, MAC for Wake-on-LAN) - none of this needs manual
// entry, and it stays current if hardware changes.
type Facts struct {
	Hostname       string `json:"hostname"`
	OS             string `json:"os"`
	Arch           string `json:"arch"`
	Manufacturer   string `json:"manufacturer,omitempty"`
	Model          string `json:"model,omitempty"`
	GPU            string `json:"gpu,omitempty"`
	VRAMTotalMiB   int    `json:"vram_total_mib,omitempty"`
	MAC            string `json:"mac,omitempty"`
	LocalIP        string `json:"local_ip,omitempty"`
	OllamaVersion  string `json:"ollama_version,omitempty"`
	OllamaURL      string `json:"ollama_url"`
	AgentVersion   string `json:"agent_version"`
	OllamaManaged  *bool  `json:"ollama_managed,omitempty"`   // Ollama is a child of this agent (router then offers Ollama updates, 0.12.1)
	OllamaPush     bool   `json:"ollama_push,omitempty"`      // Agent reports Ollama status itself (frame OLLAMA, 0.14.0)
	OllamaSwapGate bool   `json:"ollama_swap_gate,omitempty"` // Ollama update swaps only after router release (0.15.0)
	// UpstreamAPI: "openai" when the upstream is an OpenAI-compatible server (tunnel.upstream_api); left out for Ollama, so
	// a router that does not know the field sees the same facts as before.
	UpstreamAPI string `json:"upstream_api,omitempty"`
	// UpstreamRelease: the agent can stop and restart its OpenAI-compatible upstream on the router's order
	// (tunnel.upstream_child, upstream_hold.go), so the router may free the GPU for a game.
	UpstreamRelease bool `json:"upstream_release,omitempty"`
}

func collectFacts(ctx context.Context, routerURL, upstream, api string, gpu *GPU) Facts {
	f := Facts{OS: runtime.GOOS, Arch: runtime.GOARCH, AgentVersion: version, OllamaURL: upstream}
	f.Hostname, _ = os.Hostname()
	f.Manufacturer, f.Model = systemModel()
	if gpu != nil {
		if s, err := gpu.Sample(ctx); err == nil {
			f.GPU, f.VRAMTotalMiB = s.Name, s.TotalMiB
		}
	}
	f.MAC, f.LocalIP = routeInterface(routerURL)
	if api == upstreamOpenAI {
		f.UpstreamAPI = upstreamOpenAI // no Ollama, so no /api/version to ask
		return f
	}
	f.OllamaVersion = ollamaVersion(ctx, upstream)
	return f
}

// routeInterface: MAC and IP of the interface used to reach the router (UDP "connect" selects the route,
// without sending a packet). With multiple adapters (WLAN + cable), this is the correct one for Wake-on-LAN.
func routeInterface(routerURL string) (string, string) {
	u, err := url.Parse(routerURL)
	if err != nil || u.Host == "" {
		return "", ""
	}
	host := u.Hostname()
	port := u.Port()
	if port == "" {
		port = "443"
	}
	c, err := net.DialTimeout("udp", net.JoinHostPort(host, port), 3*time.Second)
	if err != nil {
		return "", ""
	}
	defer c.Close()
	local := c.LocalAddr().(*net.UDPAddr).IP
	ifs, err := net.Interfaces()
	if err != nil {
		return "", local.String()
	}
	for _, ifc := range ifs {
		addrs, err := ifc.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			if ipn, ok := a.(*net.IPNet); ok && ipn.IP.Equal(local) {
				return strings.ToUpper(ifc.HardwareAddr.String()), local.String()
			}
		}
	}
	return "", local.String()
}

func ollamaVersion(ctx context.Context, upstream string) string {
	c := &http.Client{Timeout: 3 * time.Second}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(upstream, "/")+"/api/version", nil)
	resp, err := c.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	var v struct {
		Version string `json:"version"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&v)
	return v.Version
}
