---
status: proposed
date: 2026-11-25
decision-makers: maintainer (asked for by the model test bench, proposed by Claude)
---

# An admin unload answers once the runner memory is released

> Uses the fact of [ADR-0148](0148-the-agent-reports-ollamas-runner-processes-and-their-memory.md) (Ollama's runner
> processes in the heartbeat). Sits next to [ADR-0061](0061-quiet-a-single-node-for-a-measurement.md) (quiet for a
> measurement) on the same admin path.

## Context and Problem Statement

With ADR-0148 the router knows how many Ollama runner processes live on a node. The model test bench still has to do
the sequence itself: send `keep_alive: 0` for every loaded model, wait for an empty `/api/ps`, then watch
`/admin/state` until a heartbeat newer than that says `count: 0`, and know that an older heartbeat's 0 does not count.
Every caller that gets one step wrong (a stale 0, a node whose agent does not report runners) starts its large job too
early. The tool that has the facts should do the job and its check.

## Decision Drivers

* One call for the caller's intent ("free this node's model memory"), with the checks inside.
* Preconditions checked before anything is unloaded; a refusal says the state and the next step.
* "Released" only from a heartbeat built after the unload, never from an older one.
* Outcomes the caller can tell apart without reading text: fields with fixed values.
* No new configuration; nothing changes for anyone who does not call it.

## Considered Options

* **`POST /admin/nodes/{fp}/unload` with optional `timeout_s`: refuse up front, unload everything, wait for an empty
  `/api/ps`, then for a later heartbeat with `count` 0; answer 200 `released: true` or 503 with a fixed `reason`.**
* Only the fact in `/admin/state` (ADR-0148), the sequence left to the caller. Rejected: it keeps the sequence and its
  traps in every tool.
* Unload one named model and wait until "its" runner has gone. Rejected for now: the count is node-wide (ADR-0148 leaves
  per-model attribution out); "count no larger than the models still loaded" would read a crashed runner of another
  model as this one's release.
* Unload without the release check where the agent does not report runners (older agent, Linux), answering only with an
  empty `/api/ps`. Rejected: the answer would look like the checked one while it is the old guess. Such a node is refused
  with 409; the caller can still unload through Ollama and knows it is guessing.
* Hold the node empty afterwards (block loads until the caller says so). Rejected: that is a different decision about
  routing, with its own traps; quiet (ADR-0061) already stops the router's own prewarming, and a caller that needs the
  node to itself takes it out of rotation.

## Decision Outcome

Chosen option: "`POST /admin/nodes/{fp}/unload`", because it turns the test bench's sequence into one checked call and
never answers "released" without the fact.

* Router 0.44.0, `router/skirnir_router/unload.py`, behind the control port's login, `X-Skirnir-UI` header and
  `admin_networks` like every admin write. Body optional: `{"timeout_s": 1..600}`, default 120.
* Before acting it answers 409 with `error`, `next`, the node's state and `ollama_runners` when the node is offline, its
  upstream is an OpenAI-compatible server, its agent does not report runner processes (or its last heartbeat is stale),
  an unload already runs on it (one at a time per node, so held-open requests cannot multiply the polling through the
  node's tunnel), or a measurement or benchmark runs on it. Unknown fingerprint 404, node not approved or not active
  409, a body that is not a JSON object (also malformed JSON, which is not taken as "no body") 400. None of these sends
  anything to Ollama.
* Then it sends `keep_alive: 0` for every model `/api/ps` lists, polls `/api/ps` until it is empty, and waits until a
  heartbeat received after that reports `count` 0 (the router's receive time against the moment `/api/ps` was empty).
* 200 `{"ok": true, "released": true, "reason": "released", "unloaded", "waited_s", "ollama_runners"}`.
* 503 with `released: false`, `error`, `next` and one fixed `reason`: `timeout` (a model is still listed or a runner
  still lives at the deadline; the answer names `loaded`), `not_reported` (the agent stopped reporting runners, for
  example because Ollama restarted) or `offline`. A failed Ollama call answers 502 as the measurement does.
* Each answer that got as far as unloading writes a journal entry `unload` and a log line.

### Consequences

* Good, because the test bench makes one call and starts its job on 200, and the router, not each tool, knows that an
  older heartbeat's 0 does not count.
* Good, because a node that cannot confirm the release is refused before anything happens, with the reason.
* Bad, because the answer is a moment: a request that arrives afterwards loads a model again. The caller still has to
  keep traffic away if it needs the node empty for longer.
* Bad, because the request is held open for up to `timeout_s` (600 s at most); a client must allow that.
* Bad, because the node-wide count makes the unload all or nothing; one model cannot be freed alone with this check.

### Confirmation

`test/selftest_ollama_runners.py` (CI): `timeout_s` outside 1 to 600 or of the wrong type and a malformed JSON body are
refused; an offline node, an OpenAI-compatible upstream, a node without the fact, a second unload of the same node while
one runs and a node with a running measurement or benchmark are refused with 409 before Ollama is asked, and a cancelled
unload does not leave the node marked; every loaded model gets `keep_alive: 0`; the answer comes with the heartbeat that reports no
runner, not when `/api/ps` empties, and an older heartbeat's 0 does not count (a mutation that drops this check fails the
test); a runner that stays, a model that stays listed, a vanished fact and a node going offline answer `released: false`
with `timeout`, `timeout` plus `loaded`, `not_reported` and `offline`; a failing `/api/ps` answers 502; over HTTP 200,
503, 404, 409 and 400 as above, a journal entry is written, the route wins over the generic node action, and requests
without login or without the CSRF header are refused without reaching Ollama.
