---
status: accepted
date: 2026-10-20
decision-makers: operator
---

# Reranking runs on a node through the tunnel and fails fast with 424

> Recorded retroactively on 2026-10-30; decided on 2026-10-20 (v0.14.5, router 0.8.5, agent 0.18.0). Source:
> `design/rerank.md`.

## Context and Problem Statement

A memory service reranks its recall results on CPU, which is slow. A GPU node (or its integrated GPU) could do it much
faster, but nodes sleep, play games or are offline, and the memory service's client retries every 5xx and 429 three times
with backoff and has no circuit breaker.

## Decision Drivers

* Use a node's accelerator when it is there.
* When it is not, the caller must fall back to its own CPU reranker at once, not after seconds of retries.

## Considered Options

* `POST /rerank` (TEI wire format) on the router, passed through the agent tunnel to a reranker service on a node (a tunnel
  route); when no reranker is usable, answer `424` with a reason code and no `Retry-After`
* Answer `503` like other refusals
* Run the reranker on the router host

## Decision Outcome

Chosen option: "Tunnel route with 424", because the caller does not retry other 4xx and moves to the next reranker of its
chain immediately; the reason (`node_offline`, `gpu_busy`, `timeout`, …) stays machine-readable. It is off by default
(404) and needs the client right `rerank`.

### Consequences

* Good, because recall gets accelerator reranking when available and loses no time when not.
* Bad, because `424` deviates from the router's usual `503` for "no node" (ADR-0024); the reason is the caller's retry
  behaviour.

### Confirmation

Selftests for the 424 reasons and validation (400/413); `design/rerank.md` documents the codes.
