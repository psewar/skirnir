---
status: proposed
date: 2026-11-16
decision-makers: operator
---

# The interactive fallback does not evict the answering model

> Source: the re-measurement of [ADR-0096](0096-a-loading-tier-comes-before-a-cold-lower-tier.md) on 2026-11-16 13:35:
> five voice-assistant questions during a node's switch from an OpenAI-compatible server back to its Ollama. Router
> 0.32.0 was rolled back to 0.31.1 after it.

## Context and Problem Statement

Interactive requests (voice commands) do not queue behind a busy warm model if another local tier can serve: the
scheduler's interactive fallback takes a free warm tier, then a free cold one, other nodes before the busy one (0.6.6).
A cold tier on the busy node is allowed when it is the only one that can serve.

The VRAM budget (`Node.budget_gib`) counts everything Ollama holds as evictable. For a cold fallback on the busy node
that includes the model that is answering the request the fallback avoids waiting for. In the measurement the role's
first model (17.7 GiB) had just been loaded and was answering one request (`max_parallel 1`); a voice command took the
fallback and loaded a 20.7 GiB lower tier on the same node. Ollama could not keep both, waited for the first model's
request to end (3 s later) and evicted it. The requests that were waiting for the first model then waited for the lower
tier, reached their 60 s interactive deadline and went to the cloud tier: six cloud answers, 65 to 78 s per question,
against none and 57 to 66 s with 0.31.1, where the same fallback happened to pick a small tier that fitted beside.

The fallback trades a short wait for one request against the role's first model for every request after it.

## Decision Drivers

* A voice command still must not wait a minute behind a long request when a tier can serve without harm.
* The model that is answering stays loaded; evicting it costs a reload (seconds to a minute) for all requests after it.
* No configuration key; the check uses figures the router already has (loaded models, measured occupancy, inflight).

## Considered Options

1. **A cold fallback on the busy node only if it fits beside the answering models** (budget minus their occupancy).
2. No cold fallback on the busy node at all (warm fallbacks and other nodes only).
3. Leave it; rely on ADR-0096's wait for loads.
4. Count answering models as unevictable in the general budget (`budget_gib`) for every cold load.

## Decision Outcome

Chosen option: **1** (operator, 2026-11-16: "yes, roll back to 0.31.1 and a PR for the fix").

* `Node.answering_vram_gib()` sums the measured occupancy (as `ollama_vram_gib`) of loaded models with a request in
  flight on the node (same model or blob).
* In the interactive fallback a cold candidate on a node whose warm model is at its limit is taken only if
  `need <= budget - answering_vram_gib`. Otherwise the fallback looks further (other tiers, other nodes) and, when nothing
  qualifies, the request takes the normal path and waits for the warm model within its interactive budget.
* Unchanged: warm fallbacks, cold fallbacks that fit beside (a small model), cold fallbacks on other nodes, and every
  request when no warm model is busy.

Option 2 was rejected because a small cold tier that fits beside the answering model harms nobody and keeps a voice
command from waiting behind a long agent request on a single-node setup. Option 3 was rejected by the measurement:
ADR-0096 waits only while a load runs; the eviction happened after the load. Option 4 was rejected for now because it
changes every cold load decision (normal path, context ladders, prewarm) and needs its own measurement; the failure seen
is specific to the fallback, which is the one path that loads beside a busy model on purpose.

### Consequences

* Good, because a voice command no longer evicts the role's first model while it answers, and the requests after it
  keep the warm model.
* Good, because the check is local to the fallback and uses existing figures.
* Bad, because on a single node a voice command now waits behind a busy first model when only a large lower tier could
  have served it. It waits within its interactive budget (60 s) as before 0.6.6 for that case.
* Neutral: Ollama may still evict an idle model for a cold load; that is the budget's existing rule.

### Confirmation

`test/selftest_fallback_keeps_busy_model.py` checks its setup (the large tier is a cold candidate because the budget
counts the answering model, the first model is at its limit), that the large cold fallback is not taken and the warm
first tier is chosen instead, that a small cold tier that fits beside, a warm fallback and a cold fallback on another node
are still taken, and that without a busy model warm first decides. With 0.32.0's scheduler the first two of those fail.
In operation: the staged questions during a forced switch again; the route journal must show no `reason: fallback` to a
cold tier that does not fit beside, no eviction of the first model and no `reason: cloud`.
