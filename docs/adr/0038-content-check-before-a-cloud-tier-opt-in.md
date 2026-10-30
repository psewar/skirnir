---
status: accepted
date: 2026-10-23
decision-makers: operator
---

# An opt-in content check lets undeclared clients reach a cloud tier

> Backfilled on 2026-10-30, decided on 2026-10-23 (v0.20.0, router 0.13.0). Source: CHANGELOG v0.20.0.
> Extends ADR-0014 without superseding it.

## Context and Problem Statement

Under ADR-0014 a client that declares no data class is `personal` and never reaches a cloud tier. For a voice assistant
whose local node can be busy with a game, that means no answer at all, although most of its questions ("what is the
weather", general knowledge) are harmless.

## Decision Drivers

* The default stays: nothing personal goes to the cloud.
* Clients that cannot declare a data class per request should still get an answer when no local node can serve.
* When unsure, stay local (fail closed).

## Considered Options

* Opt-in per client (`clients.<c>.privacy_check`) plus a global switch: when a cloud tier is chosen for such a client,
  check the request with rules (operator terms, contact data, identifiers, keyword groups) and a small classifier on the
  router host; any hit, a high probability or a classifier that does not answer keeps the request local and answers a
  configured refusal sentence; allowed requests leave with tool results redacted
* Keep cloud closed for undeclared clients (ADR-0014 alone)
* Let such clients declare `internal` and send everything

## Decision Outcome

Chosen option: "Opt-in content check, fail closed", because it keeps ADR-0014's default for everyone else and adds a
cloud path only where the operator enables it, with a check that refuses when in doubt. A declared `routing.data_class`
is never overruled. The journal records the outcome, never the text.

### Consequences

* Good, because the voice assistant can answer harmless questions while the GPU is busy.
* Bad, because the classifier is a heuristic: it may keep harmless requests local, and the rules need maintenance.

### Confirmation

`test/selftest_privacy_check.py` in CI; the classifier (`decision-embed/privacy`) was measured on a hand-written test set
of personal and neutral requests (v0.20.0 release notes).
