---
status: accepted
date: 2026-09-09
decision-makers: operator
---

# Client identity on the inference port comes first

> Backfilled on 2026-10-30, decided on 2026-09-09 (order of the build-out), enforced since 2026-09-10.
> Source: `design/roadmap.md` (decisions table, stage 2, deviation from the specification), README "Client identities on
> the inference port".

## Context and Problem Statement

The inference port had no authentication (Basic Auth existed only on the admin port). An external specification listed
64 extensions; the order of building them was open.

## Decision Drivers

* The open inference port was the only open security gap.
* Some clients can send a header, some (a flow engine node) cannot.
* Identity must not be guessable from request content.

## Considered Options

* Client identity first: Bearer token, Basic Auth or source IP per client, role and model locks, rate limits, audit log;
  `observe` first, then `enforce`
* Request semantics or the scheduler first, authentication later
* Automatic client detection from request parameters

## Decision Outcome

Chosen option: "Client identity first", because it closed the only open gap before anything else was built on top. The
identity comes only from a header or the source IP; automatic detection from request parameters was rejected as
guessable. The switch to `enforce` ran automatically after an observe phase without unknown clients.

### Consequences

* Good, because every request has a client name for limits, usage and memory grants (ADR-0027 ff.).
* Bad, because IP-bound clients are only as trustworthy as the network position; IP-only identity therefore never grants
  session or memory access.

### Confirmation

`router.client_auth.mode: enforce` in production; tests for each identity kind and for refused requests.
