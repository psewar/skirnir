---
status: proposed
date: 2026-11-26
decision-makers: maintainer
---

# Report the aggregate state of multi-project recall

## Context and Problem Statement

ADR-0030 allows one recall to search a primary project and additional readable projects.
An uninitialised primary bank can yield team facts with the primary state `bank_missing`.
Clients need to distinguish this from a lookup in which every bank is missing.

## Decision Outcome

Add `aggregate_state` to successful multi-project responses. If all banks are missing, report
`bank_missing`. Report `available` when at least one bank answered and the others answered or
are missing. Report `partial` when any secondary lookup was denied, filtered or unavailable.
The existing primary state, HTTP error policy, and per-project details keep their meanings.

Changing the meaning of `memory_state` would break clients that use it to inspect the primary bank.
Counting returned items would conflate a successful query without matches with missing storage.
