---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# The memory backend is chosen in the existing hindsight section

> Implements the selection rule of ADR-0118 for the memory backend interface of ADR-0115 (both proposed in the
> portability roadmap). Keeps ADR-0028 (optional Hindsight long-term memory) unchanged for existing installations.

## Context and Problem Statement

ADR-0115 puts long-term memory behind a `MemoryBackend` interface with a Hindsight adapter; ADR-0118 says an adapter is
chosen by a fixed name in the configuration. All memory settings live in `router.hindsight` today: endpoint and token
environment names, namespace key, timeouts, recall budgets, retention, outbox and gateway switches. Most of them are not
Hindsight-specific; they are the router's memory policy. The question is where the backend name goes and what happens to
the section, given that only one adapter exists and existing configurations must keep working without edits.

## Decision Drivers

* An existing configuration loads unchanged and behaves the same.
* One place for all memory settings; no setting that exists twice under two names.
* An unknown backend name is refused by the configuration check (`router.py --check`), not at the first request.
* No rename churn in operator configurations, documentation, the status field (`hindsight` in `/admin/context`) and the
  metric label `backend="hindsight"` before a second backend exists.

## Considered Options

* A key `backend` in the existing `router.hindsight` section, default `hindsight`.
* A new section `router.memory` with `backend`, and `router.hindsight` kept as a deprecated alias.
* No key until a second adapter exists.

## Decision Outcome

Chosen option: "A key `backend` in the existing `router.hindsight` section", because it needs no edit of any existing
configuration, keeps one place for every memory setting and makes the selection visible and checked now.

* `hindsight.backend` takes a name from `memory_backend.BACKENDS` (today only `hindsight`); the default is `hindsight`.
  Any other value fails `parse_hindsight`, so loading and `router.py --check` refuse it. `memory_backend.open_backend`
  refuses an unknown name again for configurations built in code.
* The backend name is restart-only like the endpoint (`MemoryService.active` compares the whole section), and it is not
  editable at run time.
* `endpoint_env` and `api_key_env` name the environment variables whichever adapter reads them; an adapter that needs
  more settings adds them to the same section, with the parser refusing them for other backends.

### Consequences

* Good, because no existing configuration, document or monitoring query has to change.
* Good, because a mistyped backend name stops the configuration check instead of starting an unconfigured router.
* Bad, because the section name says Hindsight even when another backend is chosen; a later rename to `router.memory`
  would be its own decision with a migration path.
* Neutral, because with one adapter the key changes nothing today; it fixes where the choice goes before a second
  adapter makes it matter.

### Confirmation

`test/selftest_memory_backend.py`, `BackendSelectionTests`: the default is `hindsight`, other names (also an import path)
are refused by the parser and by `open_backend`, and the schema accepts the key. `python router/router.py --check
router/config.example.yaml --pure` loads the example, which names the key.
