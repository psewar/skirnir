---
status: proposed
date: 2026-11-16
decision-makers: operator
---

# A loading local tier comes before the cloud

> Source: a voice-assistant role whose first tier is a local dense model and whose later tiers include a cloud model,
> right after the only online node switched from an OpenAI-compatible server back to its Ollama, 2026-11-16.
> Refines [ADR-0014](0014-cloud-as-a-role-tier-with-declared-data-classes.md) ("a cold local model still comes before
> the cloud").

## Context and Problem Statement

The first request after the switch found the role's first model cold and started loading it on the node. A request ten
seconds later found no local candidate: while a router-triggered load runs, its VRAM claim (`announce_load`) takes the
node's budget, so neither the loading model nor any other local tier fitted. The scheduler then reached the cloud tier
with no local candidate before it and treated it as ready (`reason: cloud`). The cloud model answered in 0.9 s, with a
wrong answer; the local model was ready five seconds later and served every following request.

Waiting for a loading model already exists (`_wait_for_load`, since 2026-09-25), but only when the scheduler finds no
candidate at all. A role with a cloud tier always has one, so its requests never waited and went to the cloud during
every load of its local models. That contradicts ADR-0014's order: the house rule is "local before cloud", and a model
being loaded is a cold local model a few seconds further along.

## Decision Drivers

* Local before cloud (cost, data leaving the house, answer quality of the configured first model).
* An interactive request (voice command) must not wait without bound; the admission deadline already caps every wait.
* A role that ranks a cloud tier first (or `routing.execution: cloud`) keeps going to the cloud at once.
* No new configuration key; the behaviour follows from the role's tier order.

## Considered Options

1. **Wait once for a loading local tier before a picked cloud tier**, with the existing load wait and deadline.
2. Leave it: the cloud tier is the role's declared fallback and answers fastest.
3. Count a node's loading claim as a candidate in the scheduler (a "warm soon" candidate).
4. Exclude the cloud tier while any node is loading anything.

## Decision Outcome

Chosen option: **1** (operator, 2026-11-16: "ok, implement the fix and measure again").

* When the scheduler picks a cloud tier, the router checks whether a node is loading for one of the role's local tiers
  **before** that cloud tier (`scheduler.loading_for` over those tiers). If so, the request waits for that load exactly
  like a request without a candidate: once per node and model, until the model shows as loaded or the claim is gone,
  at most until the request's admission deadline (`routing.deadline_ms`, else `max_wait_interactive_s` /
  `max_wait_s` / `max_wait_batch_s`). Then it selects again.
* After that one wait, or when the deadline is already over, the scheduler's pick stands: if the local tier is still not
  ready, the cloud tier answers as before.
* A load for a tier after the cloud tier is no reason to wait. Nothing changes when no load is running.
* The wait is counted in `routing.queued_ms` and recorded as a `wait_load` event, like the existing load wait.

Option 2 was rejected because the cloud tier is meant for when local tiers cannot serve, not for the seconds in which one
is about to. Option 3 was rejected because a candidate is something a request can be sent to now; sending to a node that
is loading would queue inside Ollama without priority and bypass the admission queue. Option 4 was rejected because a
load on an unrelated tier or node would delay requests whose local tiers cannot serve anyway.

### Consequences

* Good, because requests during a model switch stay local, as ADR-0014 intends, and a role's quality does not drop to the
  cloud model's for the seconds of a load.
* Good, because the change reuses the existing load wait, its deadline and its once-only rule; no configuration.
* Bad, because such a request now waits up to the load time (seconds for a model from RAM, up to a minute from disk)
  where it got a cloud answer at once. The admission deadline bounds it; a client that prefers speed sets
  `routing.deadline_ms`.
* Neutral: a privacy-checked cloud request is unchanged; whether a request may reach the cloud at all is still decided
  by the data class and the provider guards (ADR-0014, ADR-0038).

### Confirmation

`test/selftest_cloud_wait_for_load.py` first checks its own setup (the loading node is no candidate, the scheduler alone
picks the cloud tier), then that the request waits for the load and takes the local tier with the wait in `queued_ms`,
that a load still running at the deadline ends in the cloud tier after one wait, that an expired deadline, no running
load, and a cloud tier ranked first go to the cloud at once. Without the change four of its checks fail. In operation:
force a switch from the server back to the node's Ollama and send the role's questions staggered during the load; the
route journal must show no `reason: cloud` for them.
