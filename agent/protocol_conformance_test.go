package main

// Conformance of the Go agent with the router-agent protocol specification (docs/protocol.md, docs/protocol/*.json,
// ADR-0108). test/selftest_protocol.py checks the router and the test fake agent against the same files; this file
// checks the agent: the JSON fields of what it sends equal the documented ones (in both directions, so neither an
// undocumented field nor a documented field the agent dropped goes unnoticed), the types fit, the fields a schema calls
// required are always sent, the agent can read every documented router message, and the real login sends a hello that
// conforms and verifies.

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

const protocolDir = "../docs/protocol"

// schemaNode is the subset of JSON Schema the protocol files use (test/protocol_schema.py refuses anything else).
type schemaNode struct {
	Type                 json.RawMessage        `json:"type"`
	Properties           map[string]*schemaNode `json:"properties"`
	Required             []string               `json:"required"`
	Items                *schemaNode            `json:"items"`
	AdditionalProperties json.RawMessage        `json:"additionalProperties"`
	Ref                  string                 `json:"$ref"`
	Defs                 map[string]*schemaNode `json:"$defs"`
	Enum                 []any                  `json:"enum"`
	Const                any                    `json:"const"`
	MaxLength            *int                   `json:"maxLength"`
	Examples             []json.RawMessage      `json:"examples"`
	Legacy               bool                   `json:"x-legacy"`              // the router reads it, the current agent does not send it
	Ignored              bool                   `json:"x-ignored-by-receiver"` // informational, the receiver does not read it
	Open                 bool                   `json:"x-open"`                // content defined elsewhere (Ollama's model entries)
}

type schemaSet struct {
	t     *testing.T
	files map[string]*schemaNode
}

func loadProtocolSchemas(t *testing.T) *schemaSet {
	t.Helper()
	names, err := filepath.Glob(filepath.Join(protocolDir, "*.schema.json"))
	if err != nil || len(names) == 0 {
		t.Fatalf("no schemas in %s (%v)", protocolDir, err)
	}
	s := &schemaSet{t: t, files: map[string]*schemaNode{}}
	for _, n := range names {
		raw, err := os.ReadFile(n)
		if err != nil {
			t.Fatal(err)
		}
		var node schemaNode
		if err := json.Unmarshal(raw, &node); err != nil {
			t.Fatalf("%s: %v", n, err)
		}
		s.files[filepath.Base(n)] = &node
	}
	return s
}

// resolve follows $ref ("#/$defs/x", "other.schema.json", "other.schema.json#/$defs/x") and returns the file and node.
func (s *schemaSet) resolve(file string, n *schemaNode) (string, *schemaNode) {
	for n != nil && n.Ref != "" {
		f, ptr, _ := strings.Cut(n.Ref, "#")
		if f == "" {
			f = file
		}
		target := s.files[f]
		if target == nil {
			s.t.Fatalf("%s: unresolvable $ref %s", file, n.Ref)
		}
		if ptr != "" {
			parts := strings.Split(strings.TrimPrefix(ptr, "/"), "/")
			if len(parts) != 2 || parts[0] != "$defs" || target.Defs[parts[1]] == nil {
				s.t.Fatalf("%s: unsupported $ref %s", file, n.Ref)
			}
			target = target.Defs[parts[1]]
		}
		file, n = f, target
	}
	return file, n
}

func (n *schemaNode) types() []string {
	if len(n.Type) == 0 {
		return nil
	}
	var one string
	if json.Unmarshal(n.Type, &one) == nil {
		return []string{one}
	}
	var many []string
	_ = json.Unmarshal(n.Type, &many)
	return many
}

func (n *schemaNode) allows(typ string) bool {
	ts := n.types()
	if len(ts) == 0 {
		return true
	}
	for _, x := range ts {
		if x == typ || (x == "number" && typ == "integer") {
			return true
		}
	}
	return false
}

// additional: the additionalProperties schema, nil when it is a boolean or absent.
func (n *schemaNode) additional() *schemaNode {
	if len(n.AdditionalProperties) == 0 || n.AdditionalProperties[0] != '{' {
		return nil
	}
	var a schemaNode
	_ = json.Unmarshal(n.AdditionalProperties, &a)
	return &a
}

// validate checks a decoded JSON value: types, const, enum, maxLength, required, items and, strictly, no undocumented
// fields. Patterns and number ranges are checked by test/protocol_schema.py on the same files.
func (s *schemaSet) validate(file string, n *schemaNode, v any, path string) []string {
	file, n = s.resolve(file, n)
	var out []string
	typ := jsonType(v)
	if !n.allows(typ) {
		return []string{fmt.Sprintf("%s: %s not allowed (%v)", path, typ, n.types())}
	}
	if n.Const != nil && !reflect.DeepEqual(n.Const, v) {
		out = append(out, fmt.Sprintf("%s: want %v", path, n.Const))
	}
	if len(n.Enum) > 0 {
		found := false
		for _, e := range n.Enum {
			found = found || reflect.DeepEqual(e, v)
		}
		if !found {
			out = append(out, fmt.Sprintf("%s: %v not in %v", path, v, n.Enum))
		}
	}
	if str, ok := v.(string); ok && n.MaxLength != nil && len([]rune(str)) > *n.MaxLength {
		out = append(out, fmt.Sprintf("%s: longer than %d", path, *n.MaxLength))
	}
	if arr, ok := v.([]any); ok && n.Items != nil {
		for i, item := range arr {
			out = append(out, s.validate(file, n.Items, item, fmt.Sprintf("%s[%d]", path, i))...)
		}
	}
	if obj, ok := v.(map[string]any); ok {
		for _, r := range n.Required {
			if _, ok := obj[r]; !ok {
				out = append(out, fmt.Sprintf("%s: missing required %q", path, r))
			}
		}
		for k, val := range obj {
			switch {
			case n.Properties[k] != nil:
				out = append(out, s.validate(file, n.Properties[k], val, path+"."+k)...)
			case n.additional() != nil:
				out = append(out, s.validate(file, n.additional(), val, path+"."+k)...)
			case n.Properties != nil && !n.Open:
				out = append(out, fmt.Sprintf("%s: field %q is not in the schema", path, k))
			}
		}
	}
	return out
}

func jsonType(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case bool:
		return "boolean"
	case string:
		return "string"
	case float64:
		if x == math.Trunc(x) {
			return "integer"
		}
		return "number"
	case []any:
		return "array"
	case map[string]any:
		return "object"
	}
	return fmt.Sprintf("%T", v)
}

type jsonField struct {
	typ       reflect.Type
	omitempty bool
}

// jsonFields: the JSON names a struct marshals to.
func jsonFields(t reflect.Type) map[string]jsonField {
	out := map[string]jsonField{}
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		tag := f.Tag.Get("json")
		if tag == "-" || !f.IsExported() {
			continue
		}
		name, opts, _ := strings.Cut(tag, ",")
		if name == "" {
			name = f.Name
		}
		out[name] = jsonField{typ: f.Type, omitempty: strings.Contains(opts, "omitempty")}
	}
	return out
}

var (
	rawMessageType = reflect.TypeOf(json.RawMessage{})
	timeType       = reflect.TypeOf(time.Time{})
)

// checkType compares a Go type with a schema node. sender: the agent marshals this type, so every documented field
// (except legacy ones) must exist in Go and every required one must be sent always (no omitempty); otherwise the agent
// only reads it, and every documented field it does not ignore must exist in Go.
func (s *schemaSet) checkType(label, file string, n *schemaNode, t reflect.Type, sender bool) {
	file, n = s.resolve(file, n)
	if t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	switch {
	case t == rawMessageType:
		return // passed through unchanged (Ollama's own entries)
	case t == timeType:
		s.want(label, n, "string", t)
	case t.Kind() == reflect.Struct:
		s.want(label, n, "object", t)
		s.checkStruct(label, file, n, t, sender)
	case t.Kind() == reflect.Map:
		s.want(label, n, "object", t)
		if a := n.additional(); a != nil {
			s.checkType(label+"{}", file, a, t.Elem(), sender)
		} else {
			s.t.Errorf("%s: Go map %s, but the schema has no additionalProperties schema", label, t)
		}
	case t.Kind() == reflect.Slice:
		s.want(label, n, "array", t)
		if n.Items != nil {
			s.checkType(label+"[]", file, n.Items, t.Elem(), sender)
		}
	case t.Kind() == reflect.String:
		s.want(label, n, "string", t)
	case t.Kind() == reflect.Bool:
		s.want(label, n, "boolean", t)
	case t.Kind() >= reflect.Int && t.Kind() <= reflect.Uint64:
		s.want(label, n, "integer", t)
	case t.Kind() == reflect.Float32 || t.Kind() == reflect.Float64:
		s.want(label, n, "number", t)
	default:
		s.t.Errorf("%s: Go type %s has no JSON mapping in this test", label, t)
	}
}

func (s *schemaSet) want(label string, n *schemaNode, typ string, t reflect.Type) {
	ok := false
	for _, x := range n.types() {
		ok = ok || x == typ
	}
	if !ok && len(n.types()) > 0 {
		s.t.Errorf("%s: Go %s marshals to %s, schema says %v", label, t, typ, n.types())
	}
}

func (s *schemaSet) checkStruct(label, file string, n *schemaNode, t reflect.Type, sender bool) {
	fields := jsonFields(t)
	for name, f := range fields {
		p := n.Properties[name]
		if p == nil {
			if sender && !n.Open {
				s.t.Errorf("%s: Go sends %q (%s.%s), which the schema does not document", label, name, t.Name(), name)
			}
			continue
		}
		s.checkType(label+"."+name, file, p, f.typ, sender)
	}
	for name, p := range n.Properties {
		_, inGo := fields[name]
		switch {
		case inGo:
		case sender && !p.Legacy:
			s.t.Errorf("%s: schema documents %q, but %s never sends it (mark it x-legacy or remove it)", label, name, t.Name())
		case !sender && !p.Ignored:
			s.t.Errorf("%s: schema documents %q, but %s cannot read it (mark it x-ignored-by-receiver or add the field)", label, name, t.Name())
		}
	}
	if sender {
		for _, r := range n.Required {
			if f, ok := fields[r]; ok && (f.omitempty || f.typ.Kind() == reflect.Pointer) {
				s.t.Errorf("%s: %q is required, but %s may leave it out (omitempty or pointer)", label, r, t.Name())
			}
		}
	}
}

func (s *schemaSet) root(name string) *schemaNode {
	n := s.files[name]
	if n == nil {
		s.t.Fatalf("schema %s missing", name)
	}
	return n
}

func TestProtocolFactsMatchSchema(t *testing.T) {
	s := loadProtocolSchemas(t)
	s.checkType("facts", "facts.schema.json", s.root("facts.schema.json"), reflect.TypeOf(Facts{}), true)
}

func TestProtocolOllamaStateMatchesSchema(t *testing.T) {
	s := loadProtocolSchemas(t)
	s.checkType("OLLAMA", "ollama-state.schema.json", s.root("ollama-state.schema.json"), reflect.TypeOf(OllamaState{}), true)
}

func TestProtocolPowerMatchesSchema(t *testing.T) {
	s := loadProtocolSchemas(t)
	s.checkType("POWER", "power.schema.json", s.root("power.schema.json"), reflect.TypeOf(PowerEvent{}), true)
	b, _ := json.Marshal(PowerEvent{State: powerSuspending, At: time.Now(), Via: "PBT_APMSUSPEND"})
	var v any
	_ = json.Unmarshal(b, &v)
	if errs := s.validate("power.schema.json", s.root("power.schema.json"), v, "$"); len(errs) > 0 {
		t.Fatal(errs)
	}
}

// heartbeatKeys: the keys Heartbeat.payload puts into the HB map (the payload is untyped, so the source is the truth).
func heartbeatKeys(t *testing.T) []string {
	t.Helper()
	f, err := parser.ParseFile(token.NewFileSet(), "heartbeat.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	keys := map[string]bool{}
	for _, d := range f.Decls {
		fn, ok := d.(*ast.FuncDecl)
		if !ok || fn.Name.Name != "payload" || fn.Recv == nil {
			continue
		}
		ast.Inspect(fn.Body, func(n ast.Node) bool {
			add := func(e ast.Expr) {
				if lit, ok := e.(*ast.BasicLit); ok && lit.Kind == token.STRING {
					k, _ := strconv.Unquote(lit.Value)
					keys[k] = true
				}
			}
			switch x := n.(type) {
			case *ast.CompositeLit:
				if _, isMap := x.Type.(*ast.MapType); isMap {
					for _, e := range x.Elts {
						if kv, ok := e.(*ast.KeyValueExpr); ok {
							add(kv.Key)
						}
					}
				}
			case *ast.AssignStmt:
				for _, l := range x.Lhs {
					if ix, ok := l.(*ast.IndexExpr); ok {
						if id, ok := ix.X.(*ast.Ident); ok && id.Name == "p" {
							add(ix.Index)
						}
					}
				}
			}
			return true
		})
	}
	out := make([]string, 0, len(keys))
	for k := range keys {
		out = append(out, k)
	}
	sort.Strings(out)
	if len(out) < 5 {
		t.Fatalf("found only %v in Heartbeat.payload; the test no longer understands heartbeat.go", out)
	}
	return out
}

func TestProtocolHeartbeatMatchesSchema(t *testing.T) {
	s := loadProtocolSchemas(t)
	hb := s.root("heartbeat.schema.json")
	sample := jsonFields(reflect.TypeOf(GPUSample{}))
	// The Go type behind each value Heartbeat.payload sets. A new key fails below until it is added here and documented.
	types := map[string]reflect.Type{
		"gpu_present":       reflect.TypeOf((*GPU).Present).Out(0),
		"gpu_util_pct":      sample["gpu_util_pct"].typ,
		"vram_total_mib":    sample["vram_total_mib"].typ,
		"vram_used_mib":     sample["vram_used_mib"].typ,
		"vram_free_mib":     sample["vram_free_mib"].typ,
		"ts":                reflect.TypeOf(time.Now().Unix()),
		"ollama_tls_sha256": reflect.TypeOf(Heartbeat{}.tlsFP),
		"sensors":           reflect.TypeOf(GPUSample{}.Sensors),
		"gpu_guard":         reflect.TypeOf((*Guard).Status).Out(0),
		"update":            reflect.TypeOf((*Updater).Report).Out(0),
		"ollama_update":     reflect.TypeOf((*OllamaUpdater).Report).Out(0),
		"ollama_version":    reflect.TypeOf((*Heartbeat).ollamaVersionCached).Out(0),
		"children_vram_mib": reflect.TypeOf((*Supervisor).childrenVRAM).Out(0),
		"game":              reflect.TypeOf((*GameWatch).Fact).Out(0),
		"memory":            reflect.TypeOf(memoryFact).Out(0),
		"routes":            reflect.TypeOf((*Routes).Report).Out(0),
		"standby_models":    reflect.TypeOf((*StandbyStore).Models).Out(0),
	}
	sent := heartbeatKeys(t)
	for _, k := range sent {
		p := hb.Properties[k]
		if p == nil {
			t.Errorf("Heartbeat.payload sends %q, which heartbeat.schema.json does not document", k)
			continue
		}
		typ, ok := types[k]
		if !ok {
			t.Errorf("heartbeat key %q: add its Go type to this test", k)
			continue
		}
		s.checkType("HB."+k, "heartbeat.schema.json", p, typ, true)
	}
	for k, p := range hb.Properties {
		if !p.Legacy && !contains(sent, k) {
			t.Errorf("heartbeat.schema.json documents %q, but Heartbeat.payload never sends it (mark it x-legacy or remove it)", k)
		}
	}
	for _, r := range hb.Required {
		t.Errorf("heartbeat.schema.json requires %q; the payload map has no way to prove it is always sent, adjust this test", r)
	}
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// TestProtocolRouterMessagesAreReadable: every documented router message decodes into the agent's types, field by
// field and for each example, without unknown fields (apart from fields marked as ignored by the receiver).
func TestProtocolRouterMessagesAreReadable(t *testing.T) {
	s := loadProtocolSchemas(t)
	receivers := map[string]reflect.Type{
		"challenge.schema.json":             reflect.TypeOf(ctlMsg{}),
		"status.schema.json":                reflect.TypeOf(ctlMsg{}),
		"control-update.schema.json":        reflect.TypeOf(ctlMsg{}),
		"control-ollama-update.schema.json": reflect.TypeOf(ctlMsg{}),
		"control-ollama-swap.schema.json":   reflect.TypeOf(ctlMsg{}),
		"control-upstream.schema.json":      reflect.TypeOf(ctlMsg{}),
		"config-bundle.schema.json":         reflect.TypeOf(Provision{}),
		"heartbeat-ack.schema.json":         reflect.TypeOf(hbResponse{}),
	}
	for file, typ := range receivers {
		root := s.root(file)
		for name, p := range root.Properties {
			if name == "config" { // ctlMsg keeps the bundle raw and decodes it as Provision (checked on its own)
				continue
			}
			f, ok := jsonFields(typ)[name]
			if !ok {
				if !p.Ignored {
					t.Errorf("%s: %q has no field in %s", file, name, typ.Name())
				}
				continue
			}
			s.checkType(file+"."+name, file, p, f.typ, false)
		}
		for i, ex := range root.Examples {
			var m map[string]any
			if err := json.Unmarshal(ex, &m); err != nil {
				t.Fatalf("%s example %d: %v", file, i, err)
			}
			for name, p := range root.Properties {
				if p.Ignored {
					delete(m, name)
				}
			}
			b, _ := json.Marshal(m)
			dec := json.NewDecoder(strings.NewReader(string(b)))
			dec.DisallowUnknownFields()
			if err := dec.Decode(reflect.New(typ).Interface()); err != nil {
				t.Errorf("%s example %d does not decode into %s: %v", file, i, typ.Name(), err)
			}
		}
	}
}

func TestProtocolFrameCodesAndPassthroughMatchIndex(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join(protocolDir, "index.json"))
	if err != nil {
		t.Fatal(err)
	}
	var idx struct {
		Frames map[string]struct {
			Code int `json:"code"`
		} `json:"frames"`
		Passthrough map[string][]string `json:"passthrough"`
		Limits      map[string]float64  `json:"limits"`
	}
	if err := json.Unmarshal(raw, &idx); err != nil {
		t.Fatal(err)
	}
	agent := map[string]byte{"REQ": tunReq, "RESP": tunResp, "DATA": tunData, "END": tunEnd, "ERR": tunErr, "CANCEL": tunCancel,
		"HB": tunHB, "HBACK": tunHBAck, "OLLAMA": tunOllama, "POWER": tunPower}
	if len(agent) != len(idx.Frames) {
		t.Errorf("index has %d frames, the agent %d", len(idx.Frames), len(agent))
	}
	for name, code := range agent {
		if f, ok := idx.Frames[name]; !ok || f.Code != int(code) {
			t.Errorf("frame %s: agent code %d, index %+v", name, code, f)
		}
	}
	if !reflect.DeepEqual(idx.Passthrough, passthroughAllowed) {
		t.Errorf("passthrough allowlist differs:\n index %v\n agent %v", idx.Passthrough, passthroughAllowed)
	}
	if idx.Limits["ollama_resync_s"] != ollamaResync.Seconds() {
		t.Errorf("ollama_resync_s: index %v, agent %v", idx.Limits["ollama_resync_s"], ollamaResync.Seconds())
	}
}

// fakeRouter answers the agent's login like the router: challenge, then the given status, and hands over the hello.
func fakeRouter(t *testing.T, nonce []byte, status map[string]any) (string, <-chan []byte) {
	t.Helper()
	hellos := make(chan []byte, 1)
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/tunnel" {
			http.NotFound(w, r)
			return
		}
		c, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		_ = c.WriteJSON(map[string]any{"t": "challenge", "nonce": base64.StdEncoding.EncodeToString(nonce), "router": "test"})
		_ = c.SetReadDeadline(time.Now().Add(5 * time.Second))
		mt, data, err := c.ReadMessage()
		if err != nil || mt != websocket.TextMessage {
			close(hellos)
			return
		}
		hellos <- data
		_ = c.WriteJSON(status)
		_, _, _ = c.ReadMessage() // until the agent closes
	}))
	t.Cleanup(srv.Close)
	return srv.URL, hellos
}

func loginTunnel(t *testing.T, routerURL string, facts Facts) *Tunnel {
	t.Helper()
	log, err := newLogger(LogCfg{Dir: t.TempDir()}, "test", false)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { log.file.Close() }) // Windows does not delete open files
	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	return newTunnel(routerURL, "http://127.0.0.1:11434", &Identity{priv: priv, pub: pub}, newHeartbeat("synthetic-node", nil, log),
		func(context.Context) Facts { return facts }, log)
}

func TestProtocolLoginSendsAConformingSignedHello(t *testing.T) {
	s := loadProtocolSchemas(t)
	managed := true
	facts := Facts{Hostname: "SYNTHETIC", OS: "windows", Arch: "amd64", Manufacturer: "Example Systems", Model: "Tower 1",
		GPU: "Example GPU", VRAMTotalMiB: 24564, MAC: "02:00:00:00:00:01", LocalIP: "192.0.2.21", OllamaVersion: "0.13.0",
		OllamaURL: "http://127.0.0.1:11434", AgentVersion: version, OllamaManaged: &managed, OllamaPush: true, OllamaSwapGate: true,
		UpstreamAPI: upstreamOpenAI, UpstreamRelease: true, UpstreamFallback: true}
	nonce := make([]byte, 32)
	for i := range nonce {
		nonce[i] = byte(i * 7)
	}
	url, hellos := fakeRouter(t, nonce, map[string]any{"t": "status", "state": "revoked", "node": "synthetic-node", "message": "test"})
	tun := loginTunnel(t, url, facts)
	if _, err := tun.session(t.Context()); err == nil {
		t.Fatal("a revoked login must end the session")
	}
	if tun.getState() != "revoked" {
		t.Fatalf("state %q after a revoked status", tun.getState())
	}
	raw, ok := <-hellos
	if !ok {
		t.Fatal("no hello")
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if errs := s.validate("hello.schema.json", s.root("hello.schema.json"), v, "$"); len(errs) > 0 {
		t.Fatalf("hello does not conform: %v", errs)
	}
	m := v.(map[string]any)
	pub, _ := base64.StdEncoding.DecodeString(m["pubkey"].(string))
	sig, _ := base64.StdEncoding.DecodeString(m["sig"].(string))
	msg := append(append(append([]byte{}, nonce...), []byte(m["node"].(string))...), pub...)
	if len(pub) != ed25519.PublicKeySize || !ed25519.Verify(pub, msg, sig) {
		t.Fatal("signature over nonce|node|pubkey does not verify")
	}
}

func TestProtocolLoginRefusesAShortNonce(t *testing.T) {
	url, hellos := fakeRouter(t, make([]byte, 8), map[string]any{"t": "status", "state": "approved"})
	tun := loginTunnel(t, url, Facts{})
	if _, err := tun.session(t.Context()); err == nil {
		t.Fatal("a challenge with fewer than 16 bytes must end the login")
	}
	if _, ok := <-hellos; ok {
		t.Fatal("the agent signed a nonce shorter than 16 bytes")
	}
}
