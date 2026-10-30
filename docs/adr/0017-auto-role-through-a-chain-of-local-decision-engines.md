---
status: accepted
date: 2026-09-16
decision-makers: operator
---

# An auto role picks a role through a chain of local decision engines

> Backfilled on 2026-10-30, decided on 2026-09-16, productive since 2026-09-17. Source:
> `design/decision-engine.md`.

## Context and Problem Statement

Some clients cannot know which role suits a request. The operator proposed a generic decision-engine abstraction with a
local, non-generative classifier as the first provider, so that a language model is only needed for hard cases.

## Decision Drivers

* Existing clients that pick their role deliberately must not change behaviour.
* Fast and local; no cloud call to route a request.
* Measurable: accuracy, latency, fallback rate, calibration.

## Considered Options

* A role `auto:latest` resolved by a chain of engines (embedding model on CPU, small LLM through the router itself,
  TF-IDF, rules); the first confident engine wins, the last decides when all are unsure
* A decision engine on every request
* A single LLM as router

## Decision Outcome

Chosen option: "An auto role resolved by a chain", because only clients that ask for `auto:latest` go through it, so there
is no regression path for clients with deliberate role choices, and those choices give real labels for training
(opt-in). Uncertainty is measured (top-1/top-2 gap, entropy, minimum probability); calibrated confidence stays empty until
a measured temperature exists. Training capture is off until a client is listed, and contexts are shortened and
anonymised.

### Consequences

* Good, because the chain still answers when a service is down (rules as the last stage).
* Good, because the result is an existing role; the normal path (tiers, capabilities, score) follows unchanged.
* Bad, because the engines and their data need maintenance outside the router (`decision-embed/`, `decision-eval/`).

### Confirmation

`decision-eval/` measures top-1/top-3, latency and calibration; the journal records each decision with engine and
fallback reason.
