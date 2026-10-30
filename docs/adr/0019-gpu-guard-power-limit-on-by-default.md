---
status: accepted
date: 2026-09-24
decision-makers: operator
---

# The agent caps the GPU's power by default to protect the 12V-2x6 connector

> Backfilled on 2026-10-30, decided on 2026-09-24 (agent 0.7.0, router 0.1.2). Source:
> `design/gpu-guard.md` sections 3 and 4 and the operator's decisions in its header.

## Context and Problem Statement

High-end cards with the 12V-2x6 connector have been reported to melt connectors after long periods at high current.
Inference nodes run for hours at full load, at night, unattended.

## Decision Drivers

* Only a lower current physically protects the connector.
* The first hour of a long run must not be unprotected.
* A disabled or failing guard must be visible.

## Considered Options

* (a) The agent sets a power limit via NVML: static (80 % of the default) plus a dynamic second stage after sustained full
  load
* (b) The router throttles (fewer parallel requests, penalty in the score)
* (c) Warnings only

## Decision Outcome

Chosen option: "(a) static and dynamic, plus (b) and (c)", because only (a) lowers the current, statically since the
damage comes from sustained current, while (b) and (c) cap duration and make failures visible. The guard is on by
default; opting out is reported as a problem (operator's decision, deviating from the draft). The agent only lowers, never
raises, the limit, and re-applies it after driver resets. Load is judged on the board power from NVML, not on GPU-Z values
that arrive over localhost (review fix in 0.9.0).

### Consequences

* Good, because the card runs at about 80 % of its power by default (for example 460 W instead of 575 W) at a small cost
  in speed.
* Bad, because setting the limit needs LocalSystem rights on Windows; without them the guard falls back to (b) and (c)
  and reports it.

### Confirmation

`agent/guard_test.go`; load test and night run measured on 2026-09-25 (`design/gpu-guard.md` 5a, 5b).
