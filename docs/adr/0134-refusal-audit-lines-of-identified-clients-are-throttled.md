---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# Refusal audit lines of identified clients are throttled

> Extends [ADR-0057](0057-bound-unauthenticated-statistics-and-audit-volume.md) (bounded unauthenticated statistics
> and audit volume) to identified clients. Threat: TM-08 (hostile load), TM-15 (audit retention).

## Context and Problem Statement

ADR-0057 bounds the audit lines of unauthenticated requests: one line per source and event kind per interval, plus a
global budget. A client with a valid token was not bounded: every refusal of its requests (`rate_limited`,
`concurrency_limited`, `body_too_large`, `forbidden`) wrote one line to the audit log and one `AUDIT` line to the
journal. A hostile in-process load test (TM-08's open next action) showed it: a client that keeps sending while its
`max_concurrent` slots are busy is refused at once, cheaply, and each refusal costs a disk write and a journal line, so
one token can grow the audit log and the journal as fast as it can send. With a size limit on the audit log
(ADR-0132) the same flood rotates older evidence out.

## Decision Drivers

* The refusal itself stays as cheap and immediate as it is (429/413/403 before any work).
* Operators still see that and how often a client was refused; nothing is silently dropped.
* One client's flood must not cost another client's audit line, and must not need a global lock or budget.
* The same pattern as ADR-0057, so the log reads alike for both.

## Considered Options

* **One audit line per identified client and event kind per minute; withheld lines are counted and reported with the
  next written line (`suppressed`); the client's counters and metrics still count every refusal.**
* A global budget for all identified clients, as for unauthenticated sources. Rejected: configured clients are few and
  known; a global budget would let one client's flood hide another client's refusals.
* Rate-limit the client harder instead. Rejected as the only control: `requests_per_minute` is optional, and
  concurrency refusals happen before and independently of it.

## Decision Outcome

Chosen option: "one line per client and event kind per minute", because it bounds the audit and journal volume of an
identified client to a few lines a minute per kind, keeps every refusal counted, and keeps clients apart.

* `auth._audit_client(name, event, ...)` replaces the direct audit calls for refusals of identified clients in the
  client middleware and in `client_allows`. It writes at most one line per `CLIENT_AUDIT_EVERY_S` (60 s) per client
  and event kind; a line written after withheld ones carries `suppressed` with their number.
* The per-client statistics keep their counters (`rate_limited`, `concurrency_limited`, `too_large`, `forbidden`),
  shown in the UI and exported as metrics; they count every refusal.
* Unauthenticated requests keep ADR-0057's rules unchanged.

### Consequences

* Good, because a flooding token can no longer grow the audit log and the journal at its sending rate.
* Good, because one client's flood does not use up another client's lines.
* Bad, because the log no longer has one line per refusal of an identified client: a second refusal of the same kind
  within a minute (for example for another path or role) appears only as a count; the counters and the next line's
  `suppressed` tell how many.

### Confirmation

`test/selftest_hostile_load.py` (in CI), over the real client middleware with a held handler: a client floods 200
requests while its 4 slots are busy; all 200 are refused with 429 before their bodies are read, never more than 4 are
inside, another client is served meanwhile, the counter shows 200 and the audit has one line; after the interval the
next refusal's line reports 199 withheld. A rate-limited flood (55 refusals) and a flood for a role the client may not
use (100 refusals) write one line each, and two clients' floods are throttled apart. Without the change all four tests
fail.
