---
status: accepted
date: 2026-11-01
decision-makers: operator
---

# Agent enrollment is bounded before a key enters the registry

> Source: `docs/security-review-2026-11-01.md` (SK-SEC-02), PR #70. Extends ADR-0009.

## Context and Problem Statement

Under ADR-0009 any agent can present a new Ed25519 key; it is stored as `pending` until the operator approves it. The
review found no limit on how many unknown keys, how large a handshake or how large tunnel messages an unapproved peer
may send, so it could fill memory and the registry file.

## Decision Drivers

* Unapproved identities are untrusted, even though they sign their own key.
* Approved agents must always be able to reconnect.
* Legitimate tunnel traffic must fit the limits.

## Considered Options

* Limits before insertion: at most 32 pending keys, at most 6 new keys per source IP within 5 minutes, a hello frame of at
  most 32 KiB, and tunnel messages of at most 2 MiB; known keys (approved, pending, revoked) are not counted
* Require a pre-shared enrollment token
* No limits (status quo)

## Decision Outcome

Chosen option: "Limits before insertion", because they cap the damage of a flood without adding a secret to distribute
(which ADR-0009 removed on purpose). The 2 MiB message limit fits the agent's framing: response bodies travel as data
frames of 32 KiB.

### Consequences

* Good, because an unapproved peer can no longer grow the registry or the router's memory without bound.
* Bad, because more than 32 pending machines at once (a large fleet rollout) need approvals in batches.

### Confirmation

`test/selftest_security.py` checks the per-IP limit and that an approved key reconnects regardless; `registry.py` holds
the limits as constants.
