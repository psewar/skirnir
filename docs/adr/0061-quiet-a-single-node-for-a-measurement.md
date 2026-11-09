---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Quiet a single node for a measurement

> Source: a test-bench run on 2026-11-09 in which 6 of 9 runs were void. Replaces an operator-side workaround that
> switched global settings.

## Context and Problem Statement

A test bench measures models on a node by talking to its Ollama directly, past the router. The router cannot tell such a
model from foreign memory: the node turns busy, the busy safety net (`unload_on_busy`) unloads the measured model, and
once the node is free again `prewarm` and the residency rule load the rank-1 model back. Each of these voids the
measurement.

The workaround switched `modes.unload_on_busy`, `modes.prewarm.on_free`, `modes.prewarm.on_online` and
`modes.prewarm.residency_idle_s` off through `PUT /admin/config`. Those are global: every other node lost its safety net
and its prewarming for the duration. Restoring depended on a local backup file of the previous values. And it had a
trap: ending it while the node was still busy made the safety net unload the model that had just been loaded.

## Decision Drivers

* Only the measured node is affected; every other node keeps its behaviour.
* Busy detection stays on, so real traffic keeps away from a node whose card the measurement holds.
* It ends by itself: a crashed or forgotten measurement must not leave a node without its safety net.
* The router reports the state as a fact (`/admin/state`, UI, journal), so nobody infers it from settings or log lines.
* Ending early, while the safety net would then unload, is refused with the facts instead of happening silently.

## Considered Options

* A per-node quiet with an expiry: `POST /admin/nodes/{fp}/quiet {"until", "reason"}` and `DELETE` to end it
* Keep switching the global settings from outside (the workaround)
* Treat a node with a registered measurement like `state.MEASURING` (the router's own measurement), which skips the whole
  busy state machine
* A `busy_ok`-like allow list of model names that never count as foreign

## Decision Outcome

Chosen option: "A per-node quiet with an expiry", because it confines the effect to one node, ends by itself and keeps
busy detection on. Skipping the whole state machine like `MEASURING` would also turn off busy detection and let real
traffic in next to the measured model. An allow list of model names would not cover the model under test, which is
by definition new.

* `POST /admin/nodes/{fp}/quiet` with `until` (epoch seconds, in the future, at most 12 h ahead) and a non-empty `reason`
  starts or extends the quiet of an approved, active node. Extending keeps the start time.
* While quiet, `busy_unload_check` and the unload on the busy transition unload nothing, `prewarm` and `residency_check`
  are skipped; each skip writes a log line with the reason (the safety net and the residency rule once per check
  interval). Busy detection is unchanged.
* The quiet ends when `until` passes (checked every tick) or on `DELETE /admin/nodes/{fp}/quiet`. The `DELETE` answers
  409 with `would_unload` and the next step while the node is busy and the safety net would then unload models, unless
  the body says `accept_unload: true`. Without a quiet it answers 200 with `was_quiet: false`.
* At the end, a free node is prewarmed once (reason `quiet_end`): no transition to free would do it later. A busy node is
  left to the safety net, as without a quiet.
* `/admin/state` shows `quiet` (`until`, `since`, `reason`, `remaining_s`) per node, the UI a badge; the journal gets
  `quiet` and `quiet_end` events.

### Consequences

* Good, because other nodes keep their safety net and prewarming during a measurement.
* Good, because a forgotten quiet ends by itself, at the latest after 12 h.
* Good, because ending too early no longer unloads the measured model silently; the caller gets the facts.
* Bad, because the quiet lives in memory only: a router restart during a measurement ends it, and the safety net may then
  unload the measured model. The test bench's own journal check still detects such a run.
* Neutral, because a free quiet node still takes real requests and may load their models; quiet stops the router's own
  housekeeping, not routing.

### Confirmation

`test/selftest_quiet.py` (in CI): request validation; with a control case each, the safety net, the busy transition,
prewarm and the residency rule do nothing while quiet and act without it; busy detection still turns the node busy;
expiry ends the quiet, journals it and prewarms a free node once; the HTTP end refuses with `would_unload` while busy,
ends with `accept_unload: true` and is idempotent; the quiet routes win over the generic node action in the real app.
