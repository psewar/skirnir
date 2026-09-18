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

// Facts: was der Agent ueber seinen Rechner weiss und dem Router beim Verbindungsaufbau mitteilt.
// Der Router legt damit den Knoten an (Name, GPU, VRAM, MAC fuer Wake-on-LAN) - nichts davon muss von Hand
// eingetragen werden, und es bleibt aktuell, wenn sich Hardware aendert.
type Facts struct {
	Hostname      string `json:"hostname"`
	OS            string `json:"os"`
	Arch          string `json:"arch"`
	Manufacturer  string `json:"manufacturer,omitempty"`
	Model         string `json:"model,omitempty"`
	GPU           string `json:"gpu,omitempty"`
	VRAMTotalMiB  int    `json:"vram_total_mib,omitempty"`
	MAC           string `json:"mac,omitempty"`
	LocalIP       string `json:"local_ip,omitempty"`
	OllamaVersion string `json:"ollama_version,omitempty"`
	OllamaURL     string `json:"ollama_url"`
	AgentVersion  string `json:"agent_version"`
}

func collectFacts(ctx context.Context, routerURL, upstream string, gpu *GPU) Facts {
	f := Facts{OS: runtime.GOOS, Arch: runtime.GOARCH, AgentVersion: version, OllamaURL: upstream}
	f.Hostname, _ = os.Hostname()
	f.Manufacturer, f.Model = systemModel()
	if gpu != nil {
		if s, err := gpu.Sample(ctx); err == nil {
			f.GPU, f.VRAMTotalMiB = s.Name, s.TotalMiB
		}
	}
	f.MAC, f.LocalIP = routeInterface(routerURL)
	f.OllamaVersion = ollamaVersion(ctx, upstream)
	return f
}

// routeInterface: MAC und IP der Schnittstelle, ueber die der Router erreicht wird (UDP-"connect" waehlt die Route,
// ohne ein Paket zu senden). Bei mehreren Adaptern (WLAN + Kabel) ist das die richtige fuer Wake-on-LAN.
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
