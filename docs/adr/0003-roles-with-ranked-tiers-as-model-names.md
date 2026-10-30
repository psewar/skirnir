---
status: accepted
date: 2026-09-04
decision-makers: operator
---

# Roles with ranked tiers are the model names clients use

> Backfilled on 2026-10-30, decided on 2026-09-04, confirmed in the roadmap of 2026-09-09. Source:
> `design/routing-algorithm.md`, `design/roadmap.md` ("Modellnamen sind Schnittstelle", "Tiers tragen Kontext und
> `busy_ok`"), README "Roles and tiers".

## Context and Problem Statement

Clients such as home automations have a model name configured in many places. Models change often (new releases, other
quantisations), and the best model may not fit right now (a game holds VRAM, another model is loaded). How do clients
name the model they want?

## Decision Drivers

* Names configured in clients must stay stable when models change.
* The router needs room to fall back to a smaller model or a smaller context when VRAM is short.
* A small model must be allowed to run during a game, a large one not.

## Considered Options

* Roles (`standard:latest`, `code:latest`, …) as ranked lists of tiers `model @ num_ctx`, optionally `busy_ok`
* Concrete model names only
* Workloads with separate requirements and model lists (as in an external specification)

## Decision Outcome

Chosen option: "Roles as ranked lists of tiers", because the role name is a stable contract for clients, and the tier
list carries exactly what the router needs to fall back: model, context size (a context ladder such as `@64k → @32k`)
and whether the tier may run on a busy node. Concrete model names stay routable as well; `workload:x` was added only as
an alias (roadmap 2026-09-09).

### Consequences

* Good, because changing a model is a change of one role, not of every client.
* Good, because the context ladder lets the router find a fit when VRAM is tight.
* Bad, because a client cannot tell from the name which model answered; the response carries routing information for
  that (ADR-0011).

### Confirmation

`/api/tags` and `/v1/models` list the roles as models; the selftests and the E2E suite route through roles.
