package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

// fetchSecret holt ein Secret per Universal Auth aus dem Secret-Store.
// Muster wie render-env.sh auf router-host; hier ohne Datei-Cache, weil der Dienst das
// Passwort nur beim (Re-)Connect braucht und bei Ausfall des Secret-Stores einfach spaeter neu versucht.
func fetchSecret(ctx context.Context, m SecretStoreCfg, key string) (string, error) {
	hc := &http.Client{Timeout: 20 * time.Second}
	login, _ := json.Marshal(map[string]string{"clientId": m.ClientID, "clientSecret": m.ClientSecret})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, m.Domain+"/api/v1/auth/universal-auth/login", bytes.NewReader(login))
	req.Header.Set("Content-Type", "application/json")
	resp, err := hc.Do(req)
	if err != nil {
		return "", fmt.Errorf("secret-store login: %w", err)
	}
	var tok struct {
		AccessToken string `json:"accessToken"`
	}
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	if resp.StatusCode != 200 || json.Unmarshal(raw, &tok) != nil || tok.AccessToken == "" {
		return "", fmt.Errorf("secret-store login: HTTP %d", resp.StatusCode)
	}
	q := url.Values{"workspaceId": {m.ProjectID}, "environment": {m.Environment}, "secretPath": {m.SecretPath}}
	req, _ = http.NewRequestWithContext(ctx, http.MethodGet, m.Domain+"/api/v3/secrets/raw/"+url.PathEscape(key)+"?"+q.Encode(), nil)
	req.Header.Set("Authorization", "Bearer "+tok.AccessToken)
	resp, err = hc.Do(req)
	if err != nil {
		return "", fmt.Errorf("secret-store secret %s: %w", key, err)
	}
	defer resp.Body.Close()
	raw, _ = io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var out struct {
		Secret struct {
			Value string `json:"secretValue"`
		} `json:"secret"`
	}
	if resp.StatusCode != 200 || json.Unmarshal(raw, &out) != nil || out.Secret.Value == "" {
		return "", fmt.Errorf("secret-store secret %s: HTTP %d", key, resp.StatusCode)
	}
	return out.Secret.Value, nil
}
