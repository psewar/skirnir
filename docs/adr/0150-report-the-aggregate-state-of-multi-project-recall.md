---
status: proposed
date: 2026-11-26
decision-makers: maintainer
---

# Report the aggregate state of multi-project recall

> Extends [ADR-0030](0030-shared-memory-scopes-and-agent-interoperability.md) (shared scopes; `recall_with` reads further
> projects next to the primary one).

## Context and Problem Statement

ADR-0030 allows one recall to search a primary project and additional readable projects. The answer's `memory_state`
describes the primary bank, and `also` lists each further project with its own state.

A primary bank that was never written (`bank_missing`) next to a team bank that answers yields team facts, but the state
still says `bank_missing`. A client cannot tell this from a lookup in which every bank is missing, or from one in which a
further project was not searched at all. To find out, it has to evaluate `also` itself.

## Decision Drivers

* A client needs one value that says whether the whole lookup was complete.
* `memory_state` keeps its meaning. Clients use it to inspect the primary bank, for example to find out that it has not
  been written yet.
* The HTTP error policy stays as it is: a failing primary bank fails the recall, a failing further project does not.
* The value is derived only from outcomes the router already has. It adds no fact and no project name.

## Considered Options

* **Chosen: an additional field `aggregate_state` in successful multi-project responses.**
* **Change the meaning of `memory_state` to describe the whole lookup.** Rejected: it would break clients that read it
  for the primary bank.
* **Derive the state from the number of items returned.** Rejected: it would confuse a successful query without matches
  with missing storage.
* **Leave it to clients to evaluate `also`.** Rejected: every client would repeat the same rule, and some would get it
  wrong.

## Decision Outcome

Chosen option: "an additional field `aggregate_state`", because it answers the question without changing what existing
fields mean.

* If all banks are missing, the value is `bank_missing`.
* It is `available` when at least one bank answered and every other one answered or is missing.
* It is `partial` when any further lookup was denied, filtered or unavailable.
* The field appears in REST and Hindsight-gateway recall answers that read further projects, and only there. A recall of
  a single project has no `aggregate_state`.

### Consequences

* Good, because a client sees in one field whether team knowledge reached it, and `memory_state` and `also` keep their
  meaning.
* Good, because the value comes from authorized outcomes only and reveals nothing new.
* Bad, because clients that need it must read a new field, and a single-project answer has none.
* Neutral: the per-project details in `also` stay the place to see which project was affected.

### Confirmation

`test/selftest_interoperability.py`:

* A missing private bank with an answering team bank gives `available`, with the team facts.
* Both banks missing gives `bank_missing` and no results.
* An unavailable further project gives `partial`, and so does a denied one (withdrawn grant), while the private answer
  stays.
