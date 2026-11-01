---
status: accepted
date: 2026-10-23
decision-makers: operator
---

# An OpenAI-compatible inference server can be a node's backend; the router holds and releases it

> Backfilled on 2026-10-30, decided on 2026-10-23 (v0.17.0: router 0.10.0, agent 0.21.0; v0.18.0: router
> 0.11.0, agent 0.22.0). Source: CHANGELOG, README "Optional OpenAI-compatible node", `router/skirnir_router/openai_node.py`.

## Context and Problem Statement

A large mixture-of-experts model ran faster in a dedicated inference server with an OpenAI-compatible API than in Ollama.
Such a server keeps its model in GPU memory for as long as the process runs and has no call to unload it, so the router's
usual way to free the card for a game (unload) does not work.

## Decision Drivers

* Use the faster server without giving up roles, admission, metrics and busy detection.
* Free the GPU for a game, as with Ollama.
* Keep the node's state a fact (ADR-0022), not inferred from failed polls.

## Considered Options

* The agent fronts the server (`tunnel.upstream_api: openai`), reports its models in Ollama form; the router translates
  requests to `/v1/chat/completions`; for a game the router orders `upstream-hold` (the agent stops the server's child
  process) and later `upstream-release`; the agent reports `upstream_state` held/starting
* Run the server outside Skirnir and add it as a cloud-like provider
* Only Ollama backends

## Decision Outcome

Chosen option: "The agent fronts the server and the router orders hold and release", because the server then behaves like
any node for clients, and stopping the process is the only way to free its memory. Capabilities come from the catalog
(no `/api/show`); the server's memory is counted once, as the agent child's.

### Consequences

* Good, because a request for the server's model goes through the same scheduling as any other.
* Bad, because a held server takes a minute to come back; ADR-0033 adds a standby Ollama for the time it is held.

### Confirmation

Selftests for the OpenAI node path and hold/release (`selftest_openai_node*`); agent tests for `upstream_hold.go`.
