package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestListManifestsUsesOllamaNames(t *testing.T) {
	root := filepath.Join(t.TempDir(), "manifests")
	for _, p := range []string{
		"registry.ollama.ai/library/qwen3.8/27b",
		"registry.ollama.ai/library/gemma4/12b",
		"registry.ollama.ai/someone/tool-model/latest",
		"hf.co/org/model-gguf/q4",
	} {
		f := filepath.Join(root, filepath.FromSlash(p))
		if err := os.MkdirAll(filepath.Dir(f), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(f, []byte("{}"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	got := listManifests(root)
	want := []string{"gemma4:12b", "hf.co/org/model-gguf:q4", "qwen3.8:27b", "someone/tool-model:latest"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	if got := listManifests(filepath.Join(t.TempDir(), "missing")); len(got) != 0 {
		t.Fatalf("missing store: %v", got)
	}
}

func TestStandbyStoreOnlyWithFallbackAndFromChildEnv(t *testing.T) {
	if s := newStandbyStore(&Config{}); s != nil {
		t.Fatal("store without tunnel.upstream_fallback")
	}
	dir := t.TempDir()
	cfg := &Config{Children: []ChildSpec{{Name: "ollama", Env: []string{"OLLAMA_HOST=127.0.0.1:11434", "OLLAMA_MODELS=" + dir}}}}
	cfg.Tunnel.UpstreamFallback = &FallbackCfg{Child: "ollama"}
	s := newStandbyStore(cfg)
	if s == nil || s.dir != dir {
		t.Fatalf("store %+v, want dir %s", s, dir)
	}
	if m := s.Models(); m == nil || len(m) != 0 {
		t.Fatalf("empty store should report an empty list, got %v", m)
	}
}
