---
status: accepted
date: 2026-09-04
decision-makers: operator
---

# Busy detection is a first-class input of the scheduler

> Recorded retroactively on 2026-10-30; decided on 2026-09-04, thresholds settled on 2026-09-08, games as a fact since
> 2026-10-05 (v0.13.0), only games that need the GPU since 2026-10-21 (v0.15.0). Source: `design/routing-algorithm.md`,
> `design/roadmap.md` ("Sie kennt das Kernproblem nicht"), CHANGELOG.

## Context and Problem Statement

The GPU nodes are desktop PCs that are also used for games. A large model next to a game makes the game stutter or
spills VRAM into system memory; an external specification for the router treated hosts simply as "available or not".

## Decision Drivers

* Games on the node always win.
* Small models (a voice assistant's) should still run during a game if they fit.
* After the game the node should be fast again without anyone acting.

## Considered Options

* A node state `offline` / `free` / `busy` derived from foreign VRAM and GPU load, with `busy_ok` tiers, unloading on busy
  and prewarm after
* Treat a gaming PC as unavailable while anyone is logged in
* Ignore other use of the GPU

## Decision Outcome

Chosen option: "A node state derived from foreign VRAM and GPU load", because it keeps the node useful for small models
during a game and protects the game from large ones. The details evolved by measurement: a learned desktop baseline
(24-h minimum), hysteresis for entering and leaving busy, the agent reporting a running game and memory pressure as facts
(ADR-0022), and since v0.15.0 only a game that actually needs the GPU makes the node busy.

### Consequences

* Good, because the same machine serves models and games without manual switching.
* Good, because prewarm after the game restores the rank-1 models.
* Bad, because the thresholds are heuristics; Windows reports no per-process VRAM, so the router works with totals and
  learned baselines.

### Confirmation

Selftests for busy entry/exit, game facts and prewarm after a game; the journal records every state change.
