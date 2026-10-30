---
status: accepted
date: 2026-09-04
decision-makers: operator
---

# A central router in front of all GPU nodes

> Backfilled on 2026-10-30, decided on 2026-09-04. Source: `design/routing-algorithm.md` (draft v0.1),
> README "What Skirnir does".

## Context and Problem Statement

Several clients (a home automation system, a flow engine, coding tools, agent frameworks) use local language models.
The GPUs sit in desktop PCs that are also used for games and may be asleep. If every client talks to one Ollama
directly, each client has to know which machine is up, which model is loaded, and what to do when the machine is busy.

## Decision Drivers

* Clients should name *what* they need, not *where* it runs.
* GPU machines are shared with other use and come and go (games, sleep, updates).
* One place for authentication, limits, logging and metrics.

## Considered Options

* A central router in front of all GPU nodes
* Clients talk to one Ollama directly
* A generic LLM proxy (model aliasing only, no knowledge of node state)

## Decision Outcome

Chosen option: "A central router in front of all GPU nodes", because only a component that knows the state of every node
(loaded models, free VRAM, foreign use of the GPU, asleep or not) can pick a node per request and keep the clients
unaware of the hardware.

### Consequences

* Good, because clients keep one address and stable names while nodes change underneath.
* Good, because node state, waking (Wake-on-LAN) and fallbacks are handled once.
* Bad, because the router is a single point of failure for all clients.
* Bad, because every request takes one extra hop.

### Confirmation

Clients are configured with the router's address only; nodes keep Ollama on localhost (ADR-0008).

## More Information

Related: ADR-0003 (roles), ADR-0004 (busy detection), ADR-0005 (one process), ADR-0008 (tunnel).
