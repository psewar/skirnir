---
status: accepted
date: 2026-09-27
decision-makers: operator
---

# Components report their state as facts; nobody infers it from errors

> Recorded retroactively on 2026-10-30; decided on 2026-09-27 as a general principle of the operator; applied in many
> releases since. Source: CHANGELOG v0.9.3 (`ollama_managed`), v0.10.0 (Ollama state from the agent), v0.13.0 (game and
> memory pressure), v0.20.1 (Hindsight health probed), v0.21.0 (sleep and wake-up), v0.16.2 (slowness), v0.24.0
> (`upstream_blocked`).

## Context and Problem Statement

The router learned from the text of an agent's error message that Ollama on a node was a tray app, not a child of the
agent. Elsewhere it inferred "node down" from failed polls while the node was only asleep, or "memory unavailable" from
silence. Each such inference was fragile and wrong in some cases.

## Decision Drivers

* A component that knows something about itself is the authority for it.
* Distinct situations need distinct, machine-readable signals.

## Considered Options

* Every component reports what it knows as an explicit fact, status field or return value (agent facts, heartbeat fields,
  OLLAMA frame fields, health probes)
* The consumer infers state from failures, timeouts and error texts

## Decision Outcome

Chosen option: "Explicit facts", because a fact is exact and survives changes of wording, while inference from failures
confuses "asleep", "busy", "not managed" and "broken". Examples: `ollama_managed`, the agent pushing Ollama's models and
reachability, `game` and memory pressure, power state (sleep, wake-up), `upstream_state` / `serving`,
`upstream_blocked`, Hindsight `/health`.

### Consequences

* Good, because the router's decisions follow what the node actually is.
* Bad, because new facts need versions on both sides; older agents keep the old, inferred behaviour until updated.

### Confirmation

Review rule: a PR that makes a component's state visible to another adds a field or fact, not a text match. Related:
ADR-0023 (no exceptions or error texts as control flow).
