---
status: accepted
date: 2026-09-30
decision-makers: operator
---

# A refused request carries a reason code and Retry-After

> Backfilled on 2026-10-30, decided on 2026-09-30 (v0.9.4 "Why no node"); codes added since (for example
> `memory_pressure` v0.13.0, `server_busy` v0.23.4). Source: CHANGELOG, `router/skirnir_router/scheduler.py`.

## Context and Problem Statement

When no node could serve, clients got "no node available" and could not tell a game on the GPU (wait a minute) from a
model no node has (waiting does not help).

## Decision Drivers

* Clients and automations should know whether and when to retry.
* The operator should see the cause without reading logs.

## Considered Options

* A base code per refusal (`gpu_busy`, `vram_full`, `memory_pressure`, `node_offline`, `node_draining`, `node_failing`,
  `model_too_large`, `model_missing`, `server_busy`, …) with `Retry-After` where waiting helps and the blockers per node
  in the response body
* A plain 503 with a message

## Decision Outcome

Chosen option: "Base codes with Retry-After", because the code is wire format a client can act on, the detail text names
the numbers (for example free versus needed VRAM), and `gpu_busy` takes precedence as the only cause that resolves on its
own. The same codes are available without a request at `GET /v1/skirnir/availability/{model}`.

### Consequences

* Good, because a voice assistant can say "the GPU is busy with a game" instead of failing silently.
* Bad, because new refusal causes need a new code, not just a new message (in line with ADR-0023).

### Confirmation

Selftests for each code and for the precedence; the codes are listed in `scheduler.py` with their `Retry-After`.
