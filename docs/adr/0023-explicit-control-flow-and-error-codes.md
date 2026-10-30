---
status: accepted
date: 2026-09-27
decision-makers: operator
---

# Explicit control flow: no exceptions as control flow, no parsing of error texts

> Backfilled on 2026-10-30, decided on 2026-09-27 as a general principle of the operator; the router's code
> was converted in four parts until 2026-10-22 (v0.15.4), error codes instead of texts in v0.15.5. Source: CHANGELOG
> v0.15.4, v0.15.5.

## Context and Problem Statement

The code used `try/except` to decide normal cases (does a file exist, is a value a number, is a node reachable) and in
places matched the text of an error to tell cases apart. A changed message or a broader exception then silently changed
behaviour.

## Decision Drivers

* Normal paths must be visible in the code.
* A case that needs distinguishing needs its own code, field or status.

## Considered Options

* Check first (existence, format, capability, availability), then act; `try/except` only for genuine exceptions that
  cannot be checked beforehand or where checking would race (files vanishing, network, foreign processes, OS locks), with
  the reason in a comment; distinct cases get codes
* Keep exceptions and message matching as control flow where convenient

## Decision Outcome

Chosen option: "Check first, codes for cases", because it makes behaviour independent of message wording and keeps the
happy and unhappy paths readable. Example: a blocked agent update is recognised by its error code, no longer by its text
(v0.15.5).

### Consequences

* Good, because refactorings of messages cannot change behaviour.
* Bad, because some checks duplicate what an exception would have caught; the remaining `except` blocks carry a comment.

### Confirmation

`test/check_control_flow.py` runs in CI: every `except` and `raise` in router code needs a marker comment
`# exception-ok: <reason>`, and searching or comparing error text inside a handler is a finding in any case. Files not yet
converted would have a per-file ratchet (`control_flow_baseline.json`) that may only go down; since router 0.8.11 all
router code is converted and the baseline is empty. The rules are spelled out in `AGENTS.md`.
