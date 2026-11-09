package main

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
)

// routerTLSConfig: TLS settings for the agent's connections to the router (tunnel, update downloads) - ADR-0070.
// Without router.ca_file nil: Go's defaults with the operating system's trust store, as before. With it: the system
// roots plus the certificates in that PEM file, for a router whose certificate comes from a private CA (a homelab without
// a public DNS name). Hostname verification stays on. An unreadable file or one without a certificate is an error, so
// `check-config` and the start refuse it instead of silently falling back to the system roots.
func routerTLSConfig(caFile string) (*tls.Config, error) {
	if caFile == "" {
		return nil, nil
	}
	pemData, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("router.ca_file: %w", err)
	}
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil { // no system store on this platform: only the operator's CA
		pool = x509.NewCertPool()
	}
	if !pool.AppendCertsFromPEM(pemData) {
		return nil, fmt.Errorf("router.ca_file %s: no PEM certificate found", caFile)
	}
	return &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}, nil
}
