---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# The agent talks to its inference server through a runtime interface

> Part of the portability roadmap, phase 2 ([design/portability.md](../../design/portability.md)). Builds on
> [ADR-0032](0032-openai-compatible-server-as-node-backend.md) (OpenAI-compatible server as node backend),
> [ADR-0021](0021-ollama-as-agent-child-updated-through-the-router.md) (Ollama as the agent's child) and
> [ADR-0073](0073-close-router-paths-to-host-privileges.md) (the agent forwards only allowed paths). Depends on the
> versioned protocol specification (ADR-0107, its own PR). Router side: ADR-0110. Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

The agent knows two upstreams, selected by the string `tunnel.upstream_api`: `ollama` (default) or `openai`
(`agent/config.go`, lines 46-49 and 73-76; validated around line 440). The string is compared wherever behaviour differs:
`agent/app.go` (lines 79 and 178), `agent/facts.go` (line 56) and the watcher, which keeps it in its `api` field
(`agent/ollamawatch.go`, line 75). There is no interface behind it.

An OpenAI-compatible server is reported to the router as if it were Ollama: `openAIEntries` in `agent/ollamawatch.go`
(around lines 271-300) turns `/v1/models` into Ollama tags and `/api/ps` entries with `size: 0`, and reads the
server-specific extras `status.value` and `meta.n_ctx` that only some servers send.

The forwarding allowlist is one fixed map, `passthroughAllowed` in `agent/passthrough.go` (lines 17-29): Ollama's
inference, listing and version paths plus `/v1/chat/completions`, `/v1/embeddings` and `/v1/models`. Paths that vLLM or
llama-server offer for inference (`/v1/completions`, rerank, audio) are refused with 403 even when the node runs such a
server. The allowlist is a security control (a compromised router cannot reach model management on the node, TM-11,
TM-14) and must stay one.

Ollama-specific functions (self-update, standby manifests, truncation detection) live next to the generic ones.

## Decision Drivers

* New inference servers without new string comparisons across the agent.
* Report what the server is and can do as facts (ADR-0022), not disguised as Ollama.
* Keep the node-side allowlist: narrow by default, owned by the runtime that knows its paths.
* Existing node configurations (`upstream_api: ollama|openai`) keep working.

## Considered Options

* A `Runtime` interface with one implementation per server type, each with its own path allowlist.
* Keep `upstream_api` and add more string values.
* Let the operator extend the allowlist freely in the node configuration.

## Decision Outcome

Chosen option: "A `Runtime` interface", because it puts each server's knowledge (listing, loaded models, version,
unload, capabilities, allowed paths) in one place and keeps the allowlist a per-runtime constant in the signed binary.

* Interface: `ListModels`, `Loaded`, `Version`, `Unload`, `Capabilities`, `AllowedPaths`.
* Implementations in the repository: `ollama`, `openai-generic` (today's `openai`), `llama-server`, `vllm`, `lmstudio`.
  Ollama's updater, standby manifests and truncation detection become optional parts of the `ollama` runtime.
* Configuration: `tunnel.upstream_api` keeps its two values as aliases (`ollama`, `openai` = `openai-generic`); new names
  select the other runtimes. An unknown name is refused by `check-config`.
* The agent reports its runtime and capabilities to the router in the facts, so the router no longer infers them from an
  Ollama-shaped model list.

### Consequences

* Good, because adding a server type is one file plus tests in `agent/`.
* Good, because each runtime forwards exactly its inference paths and nothing more.
* Good, because the router sees real facts about the upstream (type, version, loaded models with sizes where known).
* Bad, because every new runtime widens what the agent forwards; each one needs a review of its paths against TM-11.
* Bad, because the router must accept the new facts and keep accepting the Ollama-shaped report from older agents for a
  transition period.

### Confirmation

Go tests per runtime against a fake HTTP server: listing, loaded models, version and unload; a test per runtime that a
path outside its allowlist answers 403 without reaching the upstream (extending `agent/passthrough_test.go`); the existing
`agent/ollamawatch_openai_test.go` and `agent/config_upstream_test.go` pass with the aliases. `go vet` for Windows and
Linux and `go test ./...` in CI.
