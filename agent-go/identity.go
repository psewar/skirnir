package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
)

// Identity: Ed25519-Schluesselpaar des Agenten. Der Public Key ist die Identitaet gegenueber dem Router
// (Fingerprint = SHA-256 des Public Keys); der Private Key liegt DPAPI-geschuetzt (Windows) bzw. 0600 (Linux).
// Der Router kennt keinen gemeinsamen Token mehr: die Freigabe eines Knotens ist die Freigabe seines Schluessels.
type Identity struct {
	priv ed25519.PrivateKey
	pub  ed25519.PublicKey
}

func loadOrCreateIdentity(dir string, log *Logger) (*Identity, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	path := filepath.Join(dir, "agent-identity.key")
	if raw, err := os.ReadFile(path); err == nil {
		seed, err := unprotectBytes(raw)
		if err != nil {
			return nil, fmt.Errorf("Identitaet %s: %w", path, err)
		}
		if len(seed) != ed25519.SeedSize {
			return nil, fmt.Errorf("Identitaet %s: ungueltige Laenge %d", path, len(seed))
		}
		priv := ed25519.NewKeyFromSeed(seed)
		return &Identity{priv: priv, pub: priv.Public().(ed25519.PublicKey)}, nil
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	blob, err := protectBytes(priv.Seed())
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, blob, 0o600); err != nil {
		return nil, err
	}
	id := &Identity{priv: priv, pub: pub}
	log.Infof("identity: neues Schluesselpaar erzeugt, Fingerprint %s", id.Fingerprint())
	return id, nil
}

// Fingerprint: SHA-256 des rohen Public Keys, hex - so zeigt es auch die Router-UI.
func (i *Identity) Fingerprint() string {
	sum := sha256.Sum256(i.pub)
	return hex.EncodeToString(sum[:])
}

func (i *Identity) PublicB64() string { return base64.StdEncoding.EncodeToString(i.pub) }

func (i *Identity) Sign(msg []byte) string {
	return base64.StdEncoding.EncodeToString(ed25519.Sign(i.priv, msg))
}
