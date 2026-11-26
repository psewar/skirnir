---
status: proposed
date: 2026-11-26
decision-makers: maintainer
---

# Verification of an accepted memory write ends at its deadline also when its status is unreadable

> Concerns the verified delivery of the durable memory outbox (`design/shared-memory-interoperability.md`, "Verified
> delivery") and the deletion lifecycle of [ADR-0076](0076-deletion-lifecycle-across-stores-and-restores.md).

## Context and Problem Statement

The outbox checks every write the memory backend accepted until the backend reports the extraction as completed. A
readable pending state already had a deadline: after `hindsight.verify_timeout_s` (default 3600 s) since acceptance the
job became `dead` with `remote_pending`.

An unreadable status had no deadline. While the backend's status request failed (unreachable, timeout, protocol error),
the job stayed `accepted` and was checked again forever. A backend that never comes back, or that lost the operation
without saying so, kept such jobs open indefinitely: no dead letter, no signal to the operator.

## Decision Drivers

* Every accepted job ends in a state the operator can see, within a known time.
* An outcome the router cannot know must not trigger a new write: an unreadable status is not a failed extraction.
* The last thing the router learned stays visible as the reason.
* Rows from before the `accepted_at` column must neither stop the worker nor stay open forever.

## Considered Options

* **Chosen: the same deadline for an unreadable status as for a readable pending one.** After `verify_timeout_s` since
  acceptance the job becomes `dead` with the last lookup error as its reason (for example `transport`), without a
  resubmission.
* **Check an unreadable status forever (the previous behaviour).** Rejected: a backend that does not come back leaves
  jobs open without any signal.
* **Resubmit after the deadline.** Rejected: the first attempt may have written, and a second write with an unknown
  outcome of the first is not the router's decision to make. An operator retry stays possible.
* **A separate, longer deadline for an unreadable status.** Not chosen for now: one deadline is simpler to explain and to
  configure. It can come later as its own decision if backend outages longer than the deadline turn out to be common.

## Decision Outcome

Chosen option: "the same deadline", because every accepted job then reaches a visible end, and the router never writes
again on a guess.

* The clock starts at `accepted_at`. A row accepted before that column existed (PostgreSQL migration 4) counts from its
  creation, the earlier and therefore safe bound.
* The reason is the last lookup error, or `remote_pending` when the last answer was a readable pending state.
* A 403 from the status request still revokes the job at once, as before.

### Consequences

* Good, because no accepted job stays open forever, and the reason says what the router last saw.
* Good, because no write is repeated on an unknown outcome.
* Bad, because a backend outage longer than `verify_timeout_s` turns accepted writes into dead letters, which the
  gateway reports as `failed`, even if the backend finishes them later. The operator retries them
  (`POST /v1/skirnir/memory/operations/{id}/retry`) before `outbox_retention_days` drops their payload. The changelog
  names this as a possible behaviour change.
* Neutral: a deletion requested for such a job proceeds as for any dead job (ADR-0076).

### Confirmation

`test/selftest_verified_delivery.py`:

* An unreadable status keeps the job `accepted` with the lookup error until the deadline, then makes it `dead` with that
  error, with a single send.
* A row without `accepted_at` is bounded by its creation time and does not stop the worker.
