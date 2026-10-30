---
status: accepted
date: 2026-10-23
decision-makers: operator
---

# An OpenAI-compatible server runs only in a time window or on request

> Recorded retroactively on 2026-10-30; decided on 2026-10-23 (v0.23.0, router 0.16.0); the window check uses the agent's
> report since v0.23.3. Source: CHANGELOG, README "Only in a window or on request", PR #60, #63.

## Context and Problem Statement

The large server takes most of the GPU and, with its experts in system memory, tens of GiB of RAM. During the day the
operator uses the machine; at night it could serve unattended work.

## Decision Drivers

* During the day the standby Ollama should serve; the server only when someone asks for it.
* Unattended night work may use the server.
* Nothing should bring the server back by accident (prewarm, residency, end of a game).

## Considered Options

* `modes.upstream_window` (off by default): outside the window the router holds a server idle for `idle_hold_s`; prewarm,
  residency and the end of a game do not release it; inside the window a held server is released once the node is free and
  the standby idle; a request for the server's model starts it at any time
* Keep the server always on
* Switch manually

## Decision Outcome

Chosen option: "A configurable window plus on-request start", because it expresses both uses (night work, explicit
requests) without manual switching. Whether the server is held is taken from the agent's report, not from the router's
memory of its last order, which is empty after a router restart (v0.23.3). An empty window (equal start and end) means
"on request only".

### Consequences

* Good, because the server's resources are used only when wanted.
* Bad, because every start and stop moves tens of GiB; ADR-0037 adds a RAM check before a start.

### Confirmation

`test/selftest_upstream_window.py` (hold outside, release inside, no ping-pong, restart case, prewarm and residency
outside the window).
