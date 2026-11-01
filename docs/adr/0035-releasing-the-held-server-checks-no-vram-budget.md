---
status: accepted
date: 2026-10-24
decision-makers: operator
---

# Releasing a held server checks no VRAM budget

> Backfilled on 2026-10-30, decided on 2026-10-24 (v0.23.3, router 0.16.3), after an intermediate step the
> same night (v0.23.2, whole card as budget). Source: CHANGELOG v0.23.2, v0.23.3, PR #62, #63.

## Context and Problem Statement

While the standby Ollama served with a large model loaded, a request for the held server's model was refused with
`vram_full`: the router compared the server's planning figure with the VRAM left next to the standby's models. Using the
whole card as budget instead (v0.23.2) still refused it, now with `gpu_busy`: the catalog's planning figure (28.8 GiB)
exceeded the card minus reserve, desktop baseline and a small foreign share (28.5 GiB).

## Decision Drivers

* A request for the server must be able to start it.
* The release stops the standby with all its models before the server starts.
* The server sizes itself to the card (its own reserve), as it did before the hold.

## Considered Options

* A request for the held server's model is a candidate unless the node is busy (game) or short of memory; no VRAM budget
* Budget = what is left next to the standby's models (old behaviour)
* Budget = the whole card (v0.23.2)

## Decision Outcome

Chosen option: "No VRAM budget for a release", because the server's need is not something the router plans: the server
takes the card it had before, with its own reserve. Any budget built from a catalog planning figure can be tipped over by
a few hundred MiB of desktop or foreign memory. Until then, the server had only ever come back by a direct order
(prewarm, residency, window), which checked no budget either. The switch to the standby keeps its budget check, because
there the router plans Ollama models itself.

### Consequences

* Good, because a request for the server's model reliably releases it.
* Bad, because the router no longer judges the server's memory at all; ADR-0037 moves the RAM check to the agent, which
  knows the server's real need.

### Confirmation

`test/selftest_server_while_standby.py` reproduces both refusals (need above the left-over budget and above the card) and
checks that memory pressure and a game still block the release.
