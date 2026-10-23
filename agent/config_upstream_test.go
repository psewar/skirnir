package main

import "testing"

// tunnel.upstream_api: Ollama unless set; "openai" accepted; anything else is refused at load, not discovered at runtime.
func TestConfigUpstreamAPI(t *testing.T) {
	c, err := loadConfig(writeCfg(t, "router:\n  url: https://r.example.net\n"))
	if err != nil || c.Tunnel.UpstreamAPI != upstreamOllama {
		t.Fatalf("default must be ollama: %v", err)
	}
	body := "router:\n  url: https://r.example.net\ntunnel:\n  upstream: http://127.0.0.1:8080\n  upstream_api: openai\n"
	if c, err = loadConfig(writeCfg(t, body)); err != nil || c.Tunnel.UpstreamAPI != upstreamOpenAI {
		t.Fatalf("openai must be accepted: %v", err)
	}
	body = "router:\n  url: https://r.example.net\ntunnel:\n  upstream_api: vllm\n"
	if _, err = loadConfig(writeCfg(t, body)); err == nil {
		t.Fatal("unknown upstream_api accepted")
	}
}
