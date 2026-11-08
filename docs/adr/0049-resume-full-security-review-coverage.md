---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# Resume bounded full reviews across scheduled runs

ADR-0047 caps model spending per run. Consecutive scans of the same revision would otherwise revisit the first batches.
For full scans, persist completed chunk offsets by path and Git blob ID after each valid model response. Prioritize new
and changed blobs, then security anchors. Start another cycle once the eligible text has been examined. Changing the
model, destination or review policy starts fresh. List pending paths, last complete revisions and excluded objects.
Only default-branch scheduled or manually dispatched full reviews can supply a ledger for the weekly run.
Retain ledgers for 45 days and restart the cycle if none survives. Delta reviews keep their original scope and budget.

Proposed on 2026-11-06, accepted by the operator on 2026-11-08, before the first scheduled run with the ledger.
