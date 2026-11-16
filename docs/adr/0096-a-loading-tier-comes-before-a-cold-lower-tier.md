---
status: proposed
date: 2026-11-16
decision-makers: operator
---

# A loading tier comes before a cold lower tier

> Source: the re-measurement of [ADR-0095](0095-a-loading-local-tier-comes-before-the-cloud.md) on 2026-11-16: five
> voice-assistant questions sent 0/3/6/10/15 s after a node switched from an OpenAI-compatible server back to its Ollama.
> Extends ADR-0095 from cloud tiers to cold local tiers.

## Context and Problem Statement

With ADR-0095 no request went to the cloud during the load of the role's first model. One request still did not wait:
the node had room beside the load's VRAM claim for a small lower tier, so the scheduler picked that tier and the router
loaded it cold as a second model (7.6 GiB) next to the one being loaded. The request was a tool-calling conversation;
the small model made the first round (the tool call), the answer round went to the first model once it was loaded. All
five questions were answered correctly in 57 to 66 s, the one that took the lower tier no faster than the others. The
second model only took memory and, loaded at the same time, probably slowed the first model's load (36 s instead of
about 15 s seen earlier that day; not isolated).

ADR-0095 waits for a loading tier only when the pick is a cloud tier. The reasoning applies to any pick that has to be
loaded first: the request starts a load anyway, and the model ranked higher is already on its way.

## Decision Drivers

* The role's tier order states the preferred model; a lower tier is meant for when the higher ones cannot serve, not
  for the seconds before they can.
* No second model loaded beside a load in progress (memory, load time).
* The interactive fallback must stay: a voice command must not wait behind a long request on a loaded, busy model when
  a lower tier can answer at once.
* The wait stays bounded by the admission deadline and happens once.

## Considered Options

1. **Wait once before any cold pick** (cloud tier, or a local tier not loaded on its node) for a running load of the
   model of a tier ranked before it.
2. Leave lower local tiers as they are (ADR-0095 for the cloud only).
3. Wait only when the cold pick is on the same node as the load.
4. Never pick a lower tier while any load runs.

## Decision Outcome

Chosen option: **1** (operator, 2026-11-16: "yes, build the PR", after the re-measurement above).

* When the scheduler's pick is cold - a cloud tier (never warm) or a local tier whose model is not loaded on the picked
  node - the router checks whether a node is loading the model of a tier ranked before the picked one
  (`scheduler.tier_loading`: the claim's model is that tier's model or the same blob). If so, the request waits for that
  load once per node and model, at most until its admission deadline, then selects again (`_wait_for_load`, event
  `wait_load`, counted in `routing.queued_ms`).
* A lower tier that is already loaded is taken at once (warm-first). So the interactive fallback past a busy loaded
  first model, which picks a free lower tier, is unchanged: nothing is loading in that case.
* For a cloud pick the check is now the same narrower one: only a load of a higher tier's own model counts, not any load
  on a node that has the model (ADR-0095 states it that way; 0.31.1 used the broader `loading_for`, which also matched a
  load of an unrelated model). The no-candidate wait keeps the broader check, because there any load's claim may be what
  blocks the budget.

Option 2 was rejected by the measurement: the lower model saved no time in a tool-calling conversation and cost a second
model's memory. Option 3 was rejected because the order argument does not depend on the node: a cold lower model on
another node still answers with the less preferred model while the preferred one is seconds away, and the wait is bounded.
Option 4 was rejected because a warm lower tier answers without loading anything and an unrelated load is no reason to
hold a request back.

### Consequences

* Good, because during a model switch the role answers with its first model, as configured, and no second model is
  loaded beside it.
* Good, because one rule now covers cloud and local cold picks; no configuration.
* Bad, because the first answer during a load may come later than from a small cold model would have, for a request
  that would have finished entirely on the small one (a single round). The measured conversation needed the first model
  anyway. A client that prefers speed sets `routing.deadline_ms`.
* Neutral: when the load does not finish by the deadline, the cold lower tier or the cloud tier answers as before.

### Confirmation

`test/selftest_cloud_wait_for_load.py` checks its setup (the small tier fits beside the claim and the scheduler alone picks
it cold), that the request waits for the first tier's load and takes it, that a warm lower tier is taken at once, that a
load of a model that is no tier of the role is no reason to wait, and the cloud cases of ADR-0095. With 0.31.1's
`proxy.py` the two new waiting checks fail. In operation: the same staged questions during a forced switch; the route
journal must show neither `reason: cloud` nor a cold lower tier for them.
