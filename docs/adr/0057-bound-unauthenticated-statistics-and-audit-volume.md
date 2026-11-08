---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# Bound unauthenticated statistics and audit volume

> Source: TM-08 in the threat model.

## Context and Problem Statement

For every source address without a valid identity the router kept a statistics entry, without limit, and under `enforce`
it wrote an audit line for every refused request. An address spray (trivial over IPv6) or a misconfigured poller could
grow memory and fill the disk with audit lines, before any authentication.

## Decision Drivers

* State reachable before authentication must be bounded.
* The audit log must still show who tried, and that more happened than it lists.
* No change for legitimate clients.

## Considered Options

* Bounded source table (least recently seen evicted, evictions counted), one audit line per source and event kind per
  interval, and a global per-minute budget for these lines with counts of what was withheld
* Keep per-request audit lines under `enforce` (status quo)
* Drop the per-source statistics entirely

## Decision Outcome

Chosen option: "Bounded table, per-source and global audit throttling with counts", because it caps memory and disk use
regardless of the number of sources while the admin view and each written line still report totals and withheld lines.
Dropping the statistics would remove the operator's view of who is knocking.

* `MAX_UNAUTH_SOURCES` = 1024 tracked sources; `unauthenticated_evicted` in the admin view counts evictions.
* One `auth_denied`/`auth_missing` line per source and event kind every 300 s (before: every refusal under `enforce`).
* At most 120 such lines a minute over all sources; a written line carries `suppressed` (this source since its last
  line) and `suppressed_all` (withheld by the global budget).
* Per-source totals (`total`, `bad_token`) still count every request.

### Consequences

* Good, because an address spray or a poller can no longer grow memory or the audit log without bound.
* Bad, because the audit log no longer has one line per refused request; the counts in the lines and the admin view are
  the record of volume.
* Neutral, because concurrency, time, byte and cost limits for authenticated traffic stay open under TM-08.

### Confirmation

`test/selftest_security.py`: a repeating source writes one line, another event kind of the same source is written with
the withheld count, a spray of 1324 sources leaves 1024 entries, the audit lines stop at the per-minute budget and the
withheld lines and evictions are counted. Against the previous code 5 refusals wrote 5 lines and the spray kept 1325
entries and wrote 1324 lines.
