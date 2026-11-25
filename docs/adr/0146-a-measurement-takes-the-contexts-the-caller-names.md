---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude from the model test bench's wishes)
---

# A measurement takes the contexts the caller names

## Context and Problem Statement

`POST /admin/measure` writes a model's real VRAM use into the catalog. With GPU values in the agent's heartbeat it loads
the model at each context alone and reads the card (nvidia-smi). The router needs those numbers: Ollama's `/api/ps`
reports too little for some models, for example about 2.5 instead of 25.5 GiB for one hybrid model. A catalog without a
real measurement then refuses a role that would fit, or accepts one that does not.

A measurement covers 8k, 32k and every context at which a role loads the model. To measure a candidate where a role would
run it, for example at 196k, the model test bench creates a temporary role through `PUT /admin/config` and removes it
afterwards. That replaces every role at once, it is visible to clients in between, and a failure leaves the role behind.

## Decision Drivers

* Measure a candidate at the context it would run at, without touching the roles.
* A measurement that cannot do what was asked refuses, instead of measuring something else.
* Requests without the new field behave as before.

## Considered Options

* **Chosen: an optional `contexts` list in the request.** The listed contexts are measured besides the fixed ones and
  the roles' contexts.
* **A separate "measure one point, write nothing" endpoint.** Rejected: the router needs the measurement in the catalog
  anyway, and two measuring paths would share one node lock.
* **Keep the temporary role.** Rejected for the reasons above.

## Decision Outcome

Chosen option: "optional `contexts`".

* `contexts` is a list of 1 to 6 distinct integers from 2048 to 1048576. Anything else answers 400 before a node is
  chosen.
* The listed contexts join the fixed contexts and the roles' contexts. The catalog entry records every point measured
  (`vram_real_gib`).
* Only the real measurement takes them. On a node without GPU values the router could only use `/api/ps` at 8k and 32k,
  so a request with `contexts` is refused there (409) instead of measuring without them.
* The answer and the measuring state in `/admin/state` name the contexts that will be measured.

### Consequences

* Good, because a candidate is measured where it would run, without a role change that clients could see.
* Good, because the catalog then carries the real number, which a later role at that context uses.
* Bad, because each listed context adds an unload and a load to the measurement, so up to 6 contexts make it take
  longer. The bound keeps that limited.
* Neutral: the endpoint stays admin-only on the control port, like before.

### Confirmation

`test/selftest_bench_api.py`:

* The listed contexts join the fixed and the roles' contexts, and cloud tiers do not count.
* A request without `contexts` is unchanged.
* Malformed lists are refused with 400 before anything starts: empty, duplicates, out of range, not integers, too many.
* A node without GPU values is refused with 409 when `contexts` is given. Without `contexts` it is measured by `/api/ps`
  as before.
