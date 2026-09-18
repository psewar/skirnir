package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// OllamaProxy ist die TLS-Vorschaltstelle vor dem lokalen Ollama (das selbst kein TLS kann).
// Der Router spricht https://<node>:11443 mit gepinntem Zertifikat (SHA-256, per Heartbeat gemeldet) und
// Router-Token; ohne Token gibt es 401. Damit ist die GPU nur noch fuer den Router nutzbar, nicht fuer das LAN.
type OllamaProxy struct {
	cfg   ProxyCfg
	token string
	log   *Logger
	cert  tls.Certificate
	fp    string // SHA-256 hex des DER-Zertifikats

	mu       sync.Mutex
	requests int
	lastErr  string
}

func newOllamaProxy(cfg ProxyCfg, token, node string, log *Logger) (*OllamaProxy, error) {
	cert, fp, created, err := loadOrCreateCert(cfg.CertDir, node)
	if err != nil {
		return nil, fmt.Errorf("ollama-proxy Zertifikat: %w", err)
	}
	if created {
		log.Infof("ollama-proxy: neues Zertifikat erzeugt in %s", cfg.CertDir)
	}
	return &OllamaProxy{cfg: cfg, token: token, log: log, cert: cert, fp: fp}, nil
}

// loadOrCreateCert laedt ollama-proxy.crt/.key oder erzeugt ein selbstsigniertes ECDSA-P256-Zertifikat (10 Jahre).
// Keine CA: der Router pinnt den Fingerprint, den der Agent im Heartbeat meldet.
func loadOrCreateCert(dir, node string) (tls.Certificate, string, bool, error) {
	crt, key := filepath.Join(dir, "ollama-proxy.crt"), filepath.Join(dir, "ollama-proxy.key")
	if _, err := os.Stat(crt); err == nil {
		c, err := tls.LoadX509KeyPair(crt, key)
		if err != nil {
			return tls.Certificate{}, "", false, err
		}
		sum := sha256.Sum256(c.Certificate[0])
		return c, hex.EncodeToString(sum[:]), false, nil
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return tls.Certificate{}, "", false, err
	}
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", false, err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: node, Organization: []string{"ollama-router-agent"}},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().AddDate(10, 0, 0),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		DNSNames:              []string{node, "localhost"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}
	if addrs, err := net.InterfaceAddrs(); err == nil {
		for _, a := range addrs {
			if ipn, ok := a.(*net.IPNet); ok && ipn.IP.To4() != nil && !ipn.IP.IsLoopback() {
				tmpl.IPAddresses = append(tmpl.IPAddresses, ipn.IP)
			}
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &priv.PublicKey, priv)
	if err != nil {
		return tls.Certificate{}, "", false, err
	}
	keyDER, err := x509.MarshalECPrivateKey(priv)
	if err != nil {
		return tls.Certificate{}, "", false, err
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
	if err := os.WriteFile(crt, certPEM, 0o644); err != nil {
		return tls.Certificate{}, "", false, err
	}
	if err := os.WriteFile(key, keyPEM, 0o600); err != nil {
		return tls.Certificate{}, "", false, err
	}
	c, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return tls.Certificate{}, "", false, err
	}
	sum := sha256.Sum256(der)
	return c, hex.EncodeToString(sum[:]), true, nil
}

func (p *OllamaProxy) Run(ctx context.Context) {
	up, err := url.Parse(p.cfg.Upstream)
	if err != nil {
		p.log.Errorf("ollama-proxy: upstream %q: %v", p.cfg.Upstream, err)
		return
	}
	rp := httputil.NewSingleHostReverseProxy(up)
	rp.FlushInterval = -1 // Ollama streamt ndjson: jeden Chunk sofort weiterreichen
	rp.Transport = &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		ResponseHeaderTimeout: 0, // Modell-Load kann Minuten dauern
		IdleConnTimeout:       90 * time.Second,
		MaxIdleConns:          32,
	}
	director := rp.Director
	rp.Director = func(r *http.Request) {
		director(r)
		r.Host = up.Host
		r.Header.Del("X-Router-Token")
	}
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		p.mu.Lock()
		p.lastErr = err.Error()
		p.mu.Unlock()
		p.log.Warnf("ollama-proxy: %s %s: %v", r.Method, r.URL.Path, err)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprintf(w, `{"error":"ollama upstream: %s"}`, err.Error())
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if p.cfg.requireToken() && p.token != "" && r.Header.Get("X-Router-Token") != p.token {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte(`{"error":"unauthorized"}`))
			return
		}
		p.mu.Lock()
		p.requests++
		p.mu.Unlock()
		rp.ServeHTTP(w, r)
	})
	srv := &http.Server{
		Addr:              p.cfg.Listen,
		Handler:           handler,
		TLSConfig:         &tls.Config{Certificates: []tls.Certificate{p.cert}, MinVersion: tls.VersionTLS12},
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		c, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		srv.Shutdown(c)
	}()
	p.log.Infof("ollama-proxy: https://%s -> %s, Zertifikat sha256:%s, Token-Pflicht=%v", p.cfg.Listen, p.cfg.Upstream, p.fp[:16], p.cfg.requireToken())
	if err := srv.ListenAndServeTLS("", ""); err != nil && err != http.ErrServerClosed {
		p.log.Errorf("ollama-proxy: %v", err)
	}
}

type ProxyStatus struct {
	Listen      string `json:"listen"`
	Upstream    string `json:"upstream"`
	Fingerprint string `json:"tls_sha256"`
	Requests    int    `json:"requests"`
	LastError   string `json:"last_error,omitempty"`
}

func (p *OllamaProxy) Status() ProxyStatus {
	p.mu.Lock()
	defer p.mu.Unlock()
	return ProxyStatus{Listen: p.cfg.Listen, Upstream: p.cfg.Upstream, Fingerprint: p.fp, Requests: p.requests, LastError: p.lastErr}
}
