---
status: rejected
date: 2026-11-26
decision-makers: maintainer (rejected in the review of PR #217)
---

# A deletion does not proceed after an upstream cancellation

> Concerns the deletion lifecycle of [ADR-0076](0076-deletion-lifecycle-across-stores-and-restores.md).

## Context and Problem Statement

Before the outbox deletes a memory document, it asks the backend for the state of the write that created it. It deletes
only after `completed` or `failed`, so that no write of that attempt can follow and refill the document. A write that the
backend reports as `cancelled` therefore makes the deletion wait. If the status never changes, the deletion becomes a
dead letter, counted in `skirnir_memory_deletions_stuck`.

Pull request #217 proposed to delete after `cancelled` too, treating it as a terminal state like `failed`.

## Decision Drivers

* A deletion the router reports as done must not be undone by a write that follows it (ADR-0076).
* What the router treats as terminal must be what the backend guarantees, not what its status name suggests.

## Considered Options

* **Delete after `cancelled` as after `failed`.** Rejected, see below.
* **Keep waiting after `cancelled` (the existing behaviour).** Kept: a deletion that cannot be confirmed becomes a
  visible dead letter instead of a false "deleted".
* **Delete after `cancelled` and delete again after a grace period.** Not chosen: Hindsight gives no bound for how long
  a cancelled worker keeps running, so no grace period would be safe.

## Decision Outcome

Rejected: a deletion does not proceed after `cancelled`.

Hindsight documents that cancelling running work is cooperative and not immediate. The operation row is marked
`cancelled` at once, but the worker notices only at its next checkpoint, and operation types without checkpoints run to
the end. Work in progress may still finish (Hindsight documentation 1.3, "Operations", section "Cancel an operation"). Its
worker writes the final status only `WHERE status <> 'cancelled'`, so a later completion does not even show up in the
status. A document deleted after `cancelled` can therefore be filled again by the same attempt, while the router reports
it as deleted.

The router itself never cancels an upstream operation. `cancelled` only comes from an operator cancelling in Hindsight
directly, so the waiting deletion stays a rare case, and the dead letter tells the operator to look.

### Consequences

* Good, because "deleted" keeps meaning that no write of that attempt can follow.
* Bad, because the deletion of a write cancelled upstream ends as a dead letter instead of succeeding. The operator
  checks the document in the backend and deletes it there if needed.
* Neutral: verification still treats `cancelled` like `failed` and resubmits the write under a new upstream id with the
  same document id. That replaces the document rather than adding to it.

### Confirmation

`test/selftest_verified_delivery.py`: after `failed` the document is deleted; after `cancelled` the job stays
`delete_pending` with `remote_pending`, and no document is deleted. The `OpState` contract in
`router/skirnir_router/memory_backend.py` says that `cancelled` does not end an attempt's writes.
