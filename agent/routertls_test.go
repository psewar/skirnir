package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gorilla/websocket"
)

// ADR-0070: router.ca_file adds a private CA for the tunnel and for update downloads; without it the system store applies.

func writeServerCA(t *testing.T, ts *httptest.Server) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "router-ca.pem")
	data := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ts.Certificate().Raw})
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestRouterTLSConfigInputs(t *testing.T) {
	if cfg, err := routerTLSConfig(""); cfg != nil || err != nil {
		t.Fatalf("empty ca_file must mean system defaults, got %v %v", cfg, err)
	}
	if _, err := routerTLSConfig(filepath.Join(t.TempDir(), "missing.pem")); err == nil {
		t.Fatal("a missing ca_file must be an error")
	}
	empty := filepath.Join(t.TempDir(), "empty.pem")
	_ = os.WriteFile(empty, []byte("not a certificate\n"), 0o600)
	if _, err := routerTLSConfig(empty); err == nil || !strings.Contains(err.Error(), "no PEM certificate") {
		t.Fatalf("a file without a certificate must be an error, got %v", err)
	}
}

func TestTunnelDialTrustsOnlyWithCAFile(t *testing.T) {
	upgrader := websocket.Upgrader{}
	ts := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err == nil {
			c.Close()
		}
	}))
	defer ts.Close()
	url := strings.Replace(ts.URL, "https://", "wss://", 1) + "/v1/tunnel"
	if _, _, err := (&websocket.Dialer{}).Dial(url, nil); err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Fatalf("without ca_file the private certificate must be refused, got %v", err)
	}
	cfg, err := routerTLSConfig(writeServerCA(t, ts))
	if err != nil {
		t.Fatal(err)
	}
	// the dialer the tunnel builds (tunnel.go: TLSClientConfig: t.tls)
	conn, _, err := (&websocket.Dialer{TLSClientConfig: cfg}).Dial(url, nil)
	if err != nil {
		t.Fatalf("with ca_file the tunnel must connect: %v", err)
	}
	conn.Close()
}

func TestUpdateDownloadTrustsOnlyWithCAFile(t *testing.T) {
	body := []byte("synthetic agent binary")
	ts := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(body) }))
	defer ts.Close()
	sum := sha256.Sum256(body)
	want := &manifestFile{Name: "skirnir-agent-9.9.9-linux-amd64", Sha256: hex.EncodeToString(sum[:]), Size: int64(len(body))}
	dst := filepath.Join(t.TempDir(), "download")
	u := &Updater{}
	if err := u.download(context.Background(), ts.URL+"/agent/file", "synthetic-token", dst, want); err == nil {
		t.Fatal("without ca_file the download from a privately certified router must fail")
	}
	cfg, err := routerTLSConfig(writeServerCA(t, ts))
	if err != nil {
		t.Fatal(err)
	}
	u.tls = cfg
	if err := u.download(context.Background(), ts.URL+"/agent/file", "synthetic-token", dst, want); err != nil {
		t.Fatalf("with ca_file the download must succeed: %v", err)
	}
}

func TestConfigRefusesUnusableCAFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	text := "node: example-node\nrouter:\n  url: https://router.example.invalid:11435\n  ca_file: " + filepath.ToSlash(filepath.Join(dir, "missing.pem")) + "\n"
	_ = os.WriteFile(path, []byte(text), 0o600)
	if _, err := loadConfig(path); err == nil || !strings.Contains(err.Error(), "ca_file") {
		t.Fatalf("check-config must refuse a missing ca_file, got %v", err)
	}
}
