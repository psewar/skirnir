---
status: rejected
date: 2026-09-07
decision-makers: operator
---

# No CPU tier for busy nodes

> Recorded retroactively on 2026-10-30; decided on 2026-09-07 after a measurement during a game. Source: operator's
> measurement notes (`test/cpu_tier_bench.py`).

## Context and Problem Statement

While a game holds the GPU, a node only serves small `busy_ok` models on the GPU (ADR-0004). A tier running a mid-size
model on the CPU (`num_gpu: 0`) could serve during the game without touching VRAM.

## Decision Drivers

* The game must not suffer.
* Answers must arrive in a usable time.

## Considered Options

* A CPU tier on busy nodes
* Small `busy_ok` models on the GPU (status quo)

## Decision Outcome

Rejected: "A CPU tier on busy nodes". Measured during a game: a 20b model on CPU generated about 16–18 tokens/s and read
prompts at about 100–130 tokens/s, took 15 s to load and used 47–65 % CPU next to the game's own load; a 35b model did not
fit into free RAM at all. Prompt processing that slow makes longer prompts (tools, context) wait minutes, and the CPU load
competes with the game. Small `busy_ok` models on the GPU stay the way to serve during a game. GPU scheduling priority
was also checked: Windows lets a process set it only for itself, not for another process, and VRAM residency has no
priority.

### Consequences

* Good, because busy nodes keep a clear rule (only `busy_ok` tiers on the GPU).
* Bad, because large models are unavailable on a node while it is busy.

### Confirmation

`test/cpu_tier_bench.py` reproduces the measurement if hardware or models change.
