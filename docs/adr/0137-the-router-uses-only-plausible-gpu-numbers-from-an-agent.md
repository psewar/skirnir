---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The router uses only plausible GPU numbers from an agent

> Threat: TM-07 (next action "plausibility checks on reports of an approved agent"). The heartbeat schema
> (`docs/protocol/heartbeat.schema.json`, ADR-0107) already states the ranges; this decision makes the router hold to
> them.

## Context and Problem Statement

An approved agent reports its GPU every few seconds: utilisation, total, used and free VRAM, the Ollama process and
the agent's own children. The router used these numbers as sent. A negative or absurd value, or an inconsistent one
(more free VRAM than the card has), went straight into routing, the fit check and the busy detection; a compromised or
broken agent could attract requests with invented free memory or hide load. A value of the wrong type raised an
exception half-way through the heartbeat: the fields before it were applied, the rest not, and the agent got an error
answer. The schema defines the ranges, but nothing enforced them.

## Decision Drivers

* The router should never compute with a GPU number no card can have.
* A single bad value must not stop the rest of the heartbeat (game, memory, routes, update progress).
* The wire format stays as it is (no new heartbeat or answer field the agent has to know).
* Operators see when an agent reports nonsense.

## Considered Options

* **Check each GPU number against the schema's range, and free VRAM and the Ollama process against the total; drop
  what fails (unknown), apply the rest, record the dropped fields on the node and log them at most every ten minutes.**
* Refuse the whole heartbeat. Rejected: one wrong field would hide everything else the agent reports and look like an
  offline node.
* Clamp values into range. Rejected: a clamped lie (free = total) is still a lie and looks like a measurement.
* Validate the full heartbeat against the JSON schema at runtime. Not now: it needs a schema validator in the router and
  covers types, not the consistency between fields that matters here.

## Decision Outcome

Chosen option: "check, drop and record", because it keeps the router from computing with impossible numbers without
changing the protocol.

* `poll.plausible_gpu_facts` keeps `gpu_util_pct` only as a finite number 0-100 and each VRAM amount only as a finite
  number from 0 to 4 TiB; `vram_free_mib` and `ollama_proc_mib` above the total by more than 256 MiB are dropped,
  because they give the node room; `vram_used_mib` above the total only makes the node look busier (an agent could
  achieve that by not answering) and is kept, as the end-to-end suite's foreign-load scenarios send it;
  `children_vram_mib` keeps only its plausible entries.
* `poll.apply_heartbeat` applies only the kept values; a dropped one is `None`, which the router already handles as
  unknown (a node without GPU). `Node.implausible_fields` lists the dropped fields (node view); a warning names them at
  most every ten minutes per node.

### Consequences

* Good, because impossible GPU numbers no longer reach routing or the busy detection.
* Good, because a wrong type no longer stops a heartbeat half-way; the answer is the normal one.
* Bad, because a plausible lie (a believable but false load) still passes; TM-07 keeps a cross-check of reports
  against observed behaviour as an open idea.
* Neutral: the answer to such a heartbeat no longer carries `error`; agents ignore that field (`x-ignored-by-receiver`).

### Confirmation

`test/selftest_heartbeat_plausibility.py` (in CI): every schema example passes unchanged; out-of-range, wrong-type, NaN
and infinite values are dropped; free VRAM and the Ollama process above the total are dropped (rounding slack
accepted), used VRAM above it is kept; children keep only plausible entries; a heartbeat with bad numbers applies the rest, lists the dropped
fields on the node and in its view, logs once per interval, and a good heartbeat clears the list; more free VRAM than
the card has is not used. `test/selftest_protocol.py` now expects such a heartbeat to be answered normally. Without the
change the new tests fail.
