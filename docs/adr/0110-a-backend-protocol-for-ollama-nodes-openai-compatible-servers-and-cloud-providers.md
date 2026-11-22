---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# A backend protocol for Ollama nodes, OpenAI-compatible servers and cloud providers

> Part of the portability roadmap, phase 2 ([design/portability.md](../../design/portability.md)). Builds on
> [ADR-0018](0018-api-front-doors-translated-to-ollama-chat.md) (Ollama JSON as the one internal format),
> [ADR-0032](0032-openai-compatible-server-as-node-backend.md) (OpenAI-compatible server as node backend) and
> [ADR-0014](0014-cloud-as-a-role-tier-with-declared-data-classes.md) (cloud as a role tier). Depends on the pure
> node assessment of phase 1 (its own PR and ADR). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

The router sends a request to one of three kinds of target, and the kind is decided by branches spread over several
modules instead of by one interface:

* `_Relay.run` in `router/skirnir_router/proxy.py` (lines 472-482) picks `_via_cloud()` when `is_cloud`, switches to the
  standby Ollama, then `_via_openai_node()` when `node.serves_openai(model)`, else `_via_node()`.
* `CloudTarget` in `router/skirnir_router/cloud.py` (line 51) imitates a node: it sets `is_cloud = True` and
  `speaks_openai = False` (lines 54-55) and copies node attributes by hand so that scheduler and proxy code can treat it
  like one; `proxy.py` line 388 asks `getattr(node, "is_cloud", False)`.
* The branches on `is_cloud` / `speaks_openai` / `openai` appear in `nodes.py`, `proxy.py`, `poll.py`, `cloud.py` and
  `openai_node.py`.
* `_Relay` (`proxy.py`, from line 431 to the end of the class, about 570 lines) holds request state, streaming, retries
  and three separate completion paths for the three kinds.

Every new kind of target (vLLM or llama-server spoken natively, llama-swap, a CPU-only node) means a branch at each of
these places, and a fix to one completion path has to be repeated in the others. Tests cannot plug in an in-process fake
target; they start `test/fake_ollama.py`, `test/fake_cloud.py` or `test/fake_agent.py` as processes.

## Decision Drivers

* One place per kind of target; a new kind is a new implementation, not new branches.
* Keep Ollama JSON as the internal format (ADR-0018) and the data-class and egress checks of the cloud path
  (ADR-0014, ADR-0048) exactly where they apply now.
* No behaviour change for clients while the refactoring runs; each step deliverable on its own.
* An in-process fake backend for tests.

## Considered Options

* A `Backend` protocol with `OllamaBackend`, `OpenAIServerBackend` and `CloudBackend`.
* Keep the branches and extract shared helpers from the three completion paths.
* Translate everything to the OpenAI format internally and treat every target as OpenAI-compatible.

## Decision Outcome

Chosen option: "A `Backend` protocol", because it moves the kind decision to one place, keeps the internal format, and
gives tests a seam.

* Core call: `execute(path, body, model, stream) -> (info, native_chunks) | Problem`, where `native_chunks` are Ollama
  JSON dicts and `info` carries what the relay needs for accounting (tokens, cost, warm or cold, timings). Each backend
  also answers what the scheduler asks today through attributes (capabilities, whether it is a cloud tier, its data-class
  ceiling).
* Order of work, each a PR of its own: (1) bring the Ollama path to dict chunks like the other two; (2) merge the three
  completion paths in `_Relay` into one; (3) replace the branch in `_Relay.run` with the backend chosen for the target;
  (4) let `CloudTarget` stop imitating a node.
* Cloud-specific checks (data class, privacy check, credential scan, egress allowlist, budget) stay in the cloud path's
  policy layer before `execute`; the backend only transports.

### Consequences

* Good, because new kinds of targets do not touch the relay or the scheduler.
* Good, because a completion fix applies to every kind at once.
* Good, because self-tests can run the full relay against an in-process backend.
* Bad, because `_Relay` is the most-used code path; the series needs the end-to-end suite on every step.
* Bad, because during the transition both the old attributes and the protocol exist.

### Confirmation

The end-to-end suite (`test/run_tests.py`) and the existing node, OpenAI-node and cloud self-tests
(`test/selftest_openai_node*.py`, `test/selftest_cloud_*.py`) pass unchanged after each step. A new self-test runs a
request through `_Relay` against an in-process fake backend. After step (3), `is_cloud` and `serves_openai` no longer
decide the path in `proxy.py`; a grep in review confirms it.
