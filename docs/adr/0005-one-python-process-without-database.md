---
status: accepted
date: 2026-09-04
decision-makers: operator
---

# The router is one Python process without a database

> Backfilled on 2026-10-30, decided on 2026-09-04 (first deployment), split into a package on 2026-09-09.
> Source: README ("TLS everywhere, one process, one event loop, no database", "Limits and non-goals"),
> `design/roadmap.md` ("Paketschnitt jetzt? Ja, als Stufe 0").

## Context and Problem Statement

The router runs on a small services host (a few CPUs, a few GiB of RAM, no GPU). It has to multiplex streaming requests,
keep node state and serve a web UI.

## Decision Drivers

* Small footprint and few dependencies on a small host.
* Streaming and many concurrent connections without threads.
* Simple operation: one service, files next to its configuration.

## Considered Options

* One Python process with `aiohttp` (one event loop), state in memory and JSON files
* A clustered service with a database for shared state
* A Go service

## Decision Outcome

Chosen option: "One Python process with aiohttp", because one event loop handles streaming and the tunnel well, the
dependencies stay at `aiohttp`, `pyyaml`, `cryptography` (optionally `paho-mqtt`, `uvloop`), and state that must survive
restarts (registry, usage, metrics, journal) fits in small JSON files. The single 2800-line `router.py` was split into a
package on 2026-09-09 before any further growth, without behaviour changes.

### Consequences

* Good, because installation and operation stay simple.
* Bad, because it is not a cluster: queue, breaker and registry live in one process.
* Bad, because the optional memory and session features (ADR-0027 to ADR-0030) need their own stores and stay opt-in so
  that normal inference keeps having no database.

### Confirmation

`requirements.txt` holds only the listed runtime dependencies; optional ones are in separate requirement files.
