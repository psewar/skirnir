---
status: accepted
date: 2026-09-10
decision-makers: operator
---

# Scheduling, circuit breaker and admission control live in the router

> Backfilled on 2026-10-30, decided on 2026-09-10 (roadmap stage 3), extended on 2026-10-02 (models without
> parallel requests wait in the router, v0.12.5). Source: `design/roadmap.md`, README "Scheduler, circuit breaker,
> admission", CHANGELOG.

## Context and Problem Statement

With several nodes and tiers, the router has to choose a candidate per request, avoid nodes that fail, and decide what
waits when a node is full. Ollama queues requests itself, but without priorities, and some models reload their runner when
a second request arrives.

## Decision Drivers

* A loaded model should win ("warm first"); speed and error rates should count.
* Interactive requests (voice) must not wait behind batch work.
* A failing node must not take every request down with it.

## Considered Options

* A weighted score (warm, inflight, free VRAM, weight, measured speed, error rate), a circuit breaker per node, and
  admission control with priority classes, aging and deadlines in the router
* Fixed order (first fitting tier, first node) and Ollama's own queue
* A separate scheduler service

## Decision Outcome

Chosen option: "Score, breaker and admission in the router", because only the router sees all nodes and all waiting
requests. Requests wait in the router's queue with priority `interactive` / `normal` / `batch`, not in Ollama's; models
that cannot run parallel requests wait in the router instead of making Ollama reload (v0.12.5). Prewarm, `keep_alive` and
a residency rule cover what an external specification called minimum loaded time and preferred warm models.

### Consequences

* Good, because the weights are settings (UI) and the decisions are in the journal.
* Good, because a busy warm model no longer blocks interactive requests when another tier can answer at once (v0.12.6).
* Bad, because the score is a heuristic whose weights have to be tuned by observation.

### Confirmation

Selftests for score, breaker states, admission order and deadlines; `/metrics` exposes queue and breaker state.
