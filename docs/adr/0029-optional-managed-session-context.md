---
status: accepted
date: 2026-10-19
decision-makers: operator
---

# Managed session context is a separate, explicit opt-in

> Recorded retroactively on 2026-10-30; decided on 2026-10-19 (v0.14.0). Source: `design/managed-context.md`.

## Context and Problem Statement

With a session store (ADR-0027) the router could also assemble a conversation's context itself (history, budgets,
compaction) instead of the client sending it every time. Some agents (for example one that does its own compaction and
memory) must keep full control.

## Decision Drivers

* No client loses control over its context unless it asks for it.
* Stored context versions must be immutable and exportable.

## Considered Options

* Three modes: `off` (default), `client-managed` (explicit event storage only, the client keeps compaction and memory) and
  `skirnir-managed` (an additional opt-in: the router builds the context within budgets, from immutable versions)
* Router-managed context for every session
* No managed context

## Decision Outcome

Chosen option: "Three modes with `skirnir-managed` as an explicit opt-in", because it lets simple clients profit while
agents with their own memory stay untouched. The affinity key `routing.session_id` does not enable it and grants no
access.

### Consequences

* Good, because context budgets and immutable versions make reconstructions reproducible.
* Bad, because a third mode adds paths to test.

### Confirmation

Selftests and the `integration_context` test cover the modes, budgets and the default `off`.
