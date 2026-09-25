package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// Tunnel: der Agent baut eine ausgehende WebSocket-Verbindung zum Router auf und haelt sie. Der Router schickt
// seine Ollama-Aufrufe als Streams durch diese Verbindung zurueck; der Agent fuehrt sie gegen das lokale Ollama aus.
// Damit braucht ein Knoten keine eingehende Firewall-Regel, kein eigenes Zertifikat und keine feste IP.
//
// Anmeldung (Textrahmen, JSON):
//
//	Router->Agent {"t":"challenge","nonce":base64}
//	Agent->Router {"t":"hello","node":..,"pubkey":base64,"sig":base64(sign(nonce|node|pubkey)),"facts":{...}}
//	Router->Agent {"t":"status","state":"pending|approved|revoked","node":..,"config":{...}}   (spaeter auch "config")
//
// Datenrahmen (binaer): typ(1) | stream_id(4, big endian) | payload
//
//	REQ    Router->Agent  JSON {method,path,headers} + "\n" + Body
//	RESP   Agent->Router  JSON {status,headers}
//	DATA   Agent->Router  Antwort-Bytes (gestreamt, ein Rahmen pro Chunk)
//	END    Agent->Router  Antwort fertig
//	ERR    Agent->Router  Text (Upstream-Fehler)
//	CANCEL Router->Agent  Anfrage abbrechen
//	HB     Agent->Router  Heartbeat (JSON, stream 0)
//	HBACK  Router->Agent  Antwort auf den Heartbeat (JSON {state,busy_reason}, stream 0)
const (
	tunReq    byte = 1
	tunResp   byte = 2
	tunData   byte = 3
	tunEnd    byte = 4
	tunErr    byte = 5
	tunCancel byte = 6
	tunHB     byte = 7
	tunHBAck  byte = 8
)

type Tunnel struct {
	url      string
	upstream string
	log      *Logger
	http     *http.Client
	id       *Identity
	facts    func(context.Context) Facts
	hb       *Heartbeat
	onStatus func(state, node string, p *Provision)
	onUpdate func(UpdateOrder) // Auftrag {"t":"update"} vom Router

	mu        sync.Mutex
	conn      *websocket.Conn
	connected bool
	state     string // connecting | pending | approved | revoked
	nodeName  string
	since     time.Time
	requests  int
	lastErr   string
	hbEvery   time.Duration

	wmu     sync.Mutex // gorilla erlaubt nur einen Schreiber gleichzeitig
	smu     sync.Mutex
	streams map[uint32]context.CancelFunc
}

func newTunnel(routerURL, upstream string, id *Identity, hb *Heartbeat, facts func(context.Context) Facts, log *Logger) *Tunnel {
	u := strings.TrimRight(routerURL, "/")
	u = strings.Replace(u, "https://", "wss://", 1)
	u = strings.Replace(u, "http://", "ws://", 1)
	return &Tunnel{url: u + "/v1/tunnel", upstream: strings.TrimRight(upstream, "/"), log: log, id: id, hb: hb, facts: facts,
		http: &http.Client{Timeout: 0, Transport: &http.Transport{
			MaxIdleConns: 64, MaxIdleConnsPerHost: 32, IdleConnTimeout: 90 * time.Second, // Streams laufen parallel zu Ollama; Default haelt nur 2 Leerlauf-Verbindungen
			DisableCompression: true, ForceAttemptHTTP2: false, ResponseHeaderTimeout: 0}},
		streams: map[uint32]context.CancelFunc{}, state: "connecting", hbEvery: 3 * time.Second}
}

func (t *Tunnel) SetHeartbeatInterval(d time.Duration) {
	if d > 0 {
		t.mu.Lock()
		t.hbEvery = d
		t.mu.Unlock()
	}
}

func (t *Tunnel) Run(ctx context.Context) {
	backoff := []time.Duration{time.Second, 2 * time.Second, 5 * time.Second, 10 * time.Second, 30 * time.Second}
	attempt := 0
	for {
		if ctx.Err() != nil {
			return
		}
		dur, err := t.session(ctx)
		if ctx.Err() != nil {
			return
		}
		d := backoff[min(attempt, len(backoff)-1)]
		if dur > time.Minute {
			attempt = 0
		}
		attempt++
		if t.getState() == "revoked" {
			d = 5 * time.Minute // gesperrt: nicht hämmern, aber nach Freigabe von selbst zurueckkommen
		}
		t.mu.Lock()
		t.lastErr = err.Error()
		t.mu.Unlock()
		t.log.Warnf("tunnel: %v, neuer Versuch in %s", err, d)
		select {
		case <-ctx.Done():
			return
		case <-time.After(d):
		}
	}
}

type ctlMsg struct {
	T       string          `json:"t"`
	Nonce   string          `json:"nonce,omitempty"`
	State   string          `json:"state,omitempty"`
	Node    string          `json:"node,omitempty"`
	Message string          `json:"message,omitempty"`
	Config  json.RawMessage `json:"config,omitempty"`
	// t == "update" (updater.go)
	Version   string `json:"version,omitempty"`
	File      string `json:"file,omitempty"`
	URL       string `json:"url,omitempty"`
	Sha256    string `json:"sha256,omitempty"`
	Size      int64  `json:"size,omitempty"`
	Token     string `json:"token,omitempty"`
	Manifest  string `json:"manifest,omitempty"`
	Signature string `json:"signature,omitempty"`
}

func (t *Tunnel) session(ctx context.Context) (time.Duration, error) {
	start := time.Now()
	d := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, resp, err := d.DialContext(ctx, t.url, nil)
	if err != nil {
		if resp != nil {
			return 0, fmt.Errorf("dial %s: %v (HTTP %d)", t.url, err, resp.StatusCode)
		}
		return 0, fmt.Errorf("dial %s: %w", t.url, err)
	}
	defer conn.Close()
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			_ = conn.WriteControl(websocket.CloseMessage, websocket.FormatCloseMessage(websocket.CloseNormalClosure, "shutdown"), time.Now().Add(2*time.Second))
			conn.Close()
		case <-done:
		}
	}()
	conn.SetReadLimit(64 << 20)

	// --- Anmeldung: Challenge lesen, signieren, Status abwarten
	_ = conn.SetReadDeadline(time.Now().Add(15 * time.Second))
	var ch ctlMsg
	if err := conn.ReadJSON(&ch); err != nil || ch.T != "challenge" {
		return time.Since(start), fmt.Errorf("Anmeldung: keine Challenge (%v)", err)
	}
	nonce, err := base64.StdEncoding.DecodeString(ch.Nonce)
	if err != nil || len(nonce) < 16 {
		return time.Since(start), fmt.Errorf("Anmeldung: Challenge ungueltig")
	}
	facts := t.facts(ctx)
	node := t.hb.node
	msg := append(append(append([]byte{}, nonce...), []byte(node)...), t.id.pub...)
	hello := map[string]any{"t": "hello", "node": node, "pubkey": t.id.PublicB64(), "sig": t.id.Sign(msg), "facts": facts, "agent": version}
	t.wmu.Lock()
	err = conn.WriteJSON(hello)
	t.wmu.Unlock()
	if err != nil {
		return time.Since(start), fmt.Errorf("Anmeldung senden: %w", err)
	}
	var st ctlMsg
	if err := conn.ReadJSON(&st); err != nil || st.T != "status" {
		return time.Since(start), fmt.Errorf("Anmeldung: keine Statusantwort (%v)", err)
	}
	t.applyStatus(st)
	if st.State == "revoked" {
		return time.Since(start), fmt.Errorf("vom Router gesperrt (%s)", st.Message)
	}
	t.mu.Lock()
	t.conn, t.connected, t.since, t.lastErr = conn, true, time.Now(), ""
	t.mu.Unlock()
	t.log.Infof("tunnel: verbunden mit %s, Status %s (Knoten %s, Fingerprint %s)", t.url, st.State, t.getNode(), t.id.Fingerprint()[:16])

	// Heartbeat durch den Tunnel
	hbCtx, hbCancel := context.WithCancel(ctx)
	defer hbCancel()
	go t.heartbeatLoop(hbCtx, conn)

	_ = conn.SetReadDeadline(time.Now().Add(90 * time.Second))
	conn.SetPingHandler(func(data string) error { // Router pingt alle 20 s
		_ = conn.SetReadDeadline(time.Now().Add(90 * time.Second))
		return conn.WriteControl(websocket.PongMessage, []byte(data), time.Now().Add(5*time.Second))
	})
	for {
		mt, data, err := conn.ReadMessage()
		if err != nil {
			t.mu.Lock()
			t.connected, t.conn = false, nil
			t.mu.Unlock()
			t.cancelAll()
			return time.Since(start), fmt.Errorf("Verbindung: %w", err)
		}
		_ = conn.SetReadDeadline(time.Now().Add(90 * time.Second))
		if mt == websocket.TextMessage {
			var m ctlMsg
			if json.Unmarshal(data, &m) == nil {
				t.applyStatus(m)
				if m.State == "revoked" {
					return time.Since(start), fmt.Errorf("vom Router gesperrt (%s)", m.Message)
				}
			}
			continue
		}
		if mt != websocket.BinaryMessage || len(data) < 5 {
			continue
		}
		typ, sid, payload := data[0], binary.BigEndian.Uint32(data[1:5]), data[5:]
		switch typ {
		case tunReq:
			go t.handle(ctx, conn, sid, payload)
		case tunCancel:
			t.cancelStream(sid)
		case tunHBAck:
			var r hbResponse
			if json.Unmarshal(payload, &r) == nil {
				t.hb.ack(r)
			}
		}
	}
}

// applyStatus verarbeitet status/config-Nachrichten des Routers (Freigabe, Sperre, Konfigurationspaket).
func (t *Tunnel) applyStatus(m ctlMsg) {
	if m.T == "update" {
		if t.onUpdate != nil {
			t.onUpdate(UpdateOrder{Version: m.Version, File: m.File, URL: m.URL, Sha256: m.Sha256, Size: m.Size, Token: m.Token, Manifest: m.Manifest, Signature: m.Signature})
		}
		return
	}
	if m.T != "status" && m.T != "config" {
		return
	}
	var p *Provision
	if len(m.Config) > 0 {
		p = &Provision{}
		if err := json.Unmarshal(m.Config, p); err != nil {
			t.log.Warnf("tunnel: Konfigurationspaket unlesbar: %v", err)
			p = nil
		}
	}
	t.mu.Lock()
	if m.State != "" {
		t.state = m.State
	}
	if m.Node != "" {
		t.nodeName = m.Node
	}
	if p != nil && p.HeartbeatIntervalS > 0 {
		t.hbEvery = time.Duration(p.HeartbeatIntervalS * float64(time.Second))
	}
	st, nd := t.state, t.nodeName
	t.mu.Unlock()
	if m.State != "" {
		t.log.Infof("tunnel: Status %s%s", st, map[bool]string{true: " (" + m.Message + ")", false: ""}[m.Message != ""])
	}
	if t.onStatus != nil {
		t.onStatus(st, nd, p)
	}
}

func (t *Tunnel) heartbeatLoop(ctx context.Context, conn *websocket.Conn) {
	for {
		payload, err := t.hb.payload(ctx)
		if err != nil {
			t.hb.fail(err.Error())
		} else {
			b, _ := json.Marshal(payload)
			if err := t.write(conn, tunHB, 0, b); err != nil {
				return
			}
			t.hb.markSent()
		}
		t.mu.Lock()
		d := t.hbEvery
		t.mu.Unlock()
		select {
		case <-ctx.Done():
			return
		case <-time.After(d):
		}
	}
}

func (t *Tunnel) handle(ctx context.Context, conn *websocket.Conn, sid uint32, payload []byte) {
	i := bytes.IndexByte(payload, '\n')
	if i < 0 {
		_ = t.write(conn, tunErr, sid, []byte("bad request frame"))
		return
	}
	var head struct {
		Method  string            `json:"method"`
		Path    string            `json:"path"`
		Headers map[string]string `json:"headers"`
	}
	if err := json.Unmarshal(payload[:i], &head); err != nil {
		_ = t.write(conn, tunErr, sid, []byte("bad request header: "+err.Error()))
		return
	}
	body := payload[i+1:]
	rctx, cancel := context.WithCancel(ctx)
	t.smu.Lock()
	t.streams[sid] = cancel
	t.smu.Unlock()
	t.mu.Lock()
	t.requests++
	t.mu.Unlock()
	defer func() {
		cancel()
		t.smu.Lock()
		delete(t.streams, sid)
		t.smu.Unlock()
	}()
	req, err := http.NewRequestWithContext(rctx, head.Method, t.upstream+head.Path, bytes.NewReader(body))
	if err != nil {
		_ = t.write(conn, tunErr, sid, []byte(err.Error()))
		return
	}
	for k, v := range head.Headers {
		req.Header.Set(k, v)
	}
	resp, err := t.http.Do(req)
	if err != nil {
		if rctx.Err() == nil {
			_ = t.write(conn, tunErr, sid, []byte(err.Error()))
		}
		return
	}
	defer resp.Body.Close()
	hh := map[string]string{}
	for k := range resp.Header {
		hh[k] = resp.Header.Get(k)
	}
	hb, _ := json.Marshal(map[string]any{"status": resp.StatusCode, "headers": hh})
	if err := t.write(conn, tunResp, sid, hb); err != nil {
		return
	}
	buf := make([]byte, 32*1024)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			if werr := t.write(conn, tunData, sid, buf[:n]); werr != nil {
				return
			}
		}
		if err == io.EOF {
			_ = t.write(conn, tunEnd, sid, nil)
			return
		}
		if err != nil {
			if rctx.Err() == nil {
				_ = t.write(conn, tunErr, sid, []byte(err.Error()))
			}
			return
		}
	}
}

func (t *Tunnel) write(conn *websocket.Conn, typ byte, sid uint32, payload []byte) error {
	frame := make([]byte, 5+len(payload))
	frame[0] = typ
	binary.BigEndian.PutUint32(frame[1:5], sid)
	copy(frame[5:], payload)
	t.wmu.Lock()
	defer t.wmu.Unlock()
	_ = conn.SetWriteDeadline(time.Now().Add(30 * time.Second))
	return conn.WriteMessage(websocket.BinaryMessage, frame)
}

func (t *Tunnel) cancelStream(sid uint32) {
	t.smu.Lock()
	c := t.streams[sid]
	t.smu.Unlock()
	if c != nil {
		c()
	}
}

func (t *Tunnel) cancelAll() {
	t.smu.Lock()
	defer t.smu.Unlock()
	for sid, c := range t.streams {
		c()
		delete(t.streams, sid)
	}
}

func (t *Tunnel) getState() string { t.mu.Lock(); defer t.mu.Unlock(); return t.state }
func (t *Tunnel) getNode() string  { t.mu.Lock(); defer t.mu.Unlock(); return t.nodeName }

type TunnelStatus struct {
	URL       string `json:"url"`
	Connected bool   `json:"connected"`
	State     string `json:"state"`
	Node      string `json:"node,omitempty"`
	Since     string `json:"since,omitempty"`
	Requests  int    `json:"requests"`
	LastError string `json:"last_error,omitempty"`
}

func (t *Tunnel) Status() TunnelStatus {
	t.mu.Lock()
	defer t.mu.Unlock()
	s := TunnelStatus{URL: t.url, Connected: t.connected, State: t.state, Node: t.nodeName, Requests: t.requests, LastError: t.lastErr}
	if t.connected {
		s.Since = t.since.Format(time.RFC3339)
	}
	return s
}
