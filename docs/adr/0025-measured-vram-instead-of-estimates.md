---
status: accepted
date: 2026-09-30
decision-makers: operator
---

# VRAM planning uses measured values per model and context

> Backfilled on 2026-10-30, decided on 2026-09-30 (v0.9.4, v0.9.5). Source: CHANGELOG ("real VRAM numbers"),
> README "Measurements as a reference".

## Context and Problem Statement

The router decides whether a tier fits a node from the weights plus a context cost. Ollama's `/api/ps` reports too little
for some models, and the formula missed compute buffers, so the router refused context switches that actually fitted or
loaded models that spilled into system memory.

## Decision Drivers

* Fit decisions must match what the card really holds.
* Measurements should be repeatable from the UI.

## Considered Options

* Measure the real occupancy per model and context (UI button "measure", stored in the catalog as `real`) and plan with
  it; formula only as fallback
* Formula only (weights + KV cost per 1k tokens)
* Trust `/api/ps`

## Decision Outcome

Chosen option: "Measured values with formula as fallback", because the measurement includes everything the runtime
allocates. The agent also reports the GPU memory of its other children (for example a speech-to-text service), so it is
neither counted as foreign use nor learned into the desktop baseline (v0.9.6).

### Consequences

* Good, because fewer refused fits and fewer spills into system memory.
* Bad, because values must be re-measured when Ollama, the driver or the KV cache type change.

### Confirmation

Catalog entries carry `real`; the measurement procedure is in `design/routing-algorithm.md` section 5.
