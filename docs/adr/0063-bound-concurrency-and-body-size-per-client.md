---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Bound concurrency and request size per client

> Source: TM-08 in the threat model. Complements ADR-0057 (bounded unauthenticated statistics and audit volume).

## Context and Problem Statement

An authenticated client was bounded only by `requests_per_minute`, the router-wide body ceiling (64 MiB) and counts of
messages, tools and images. Nothing bounded how many of its requests were in progress at once: one client with a valid
token, or a stolen one, could fill the admission queue and the nodes with long streams while staying under its rate, and
send 64 MiB bodies the router reads into memory. Rate limits count starts, not occupancy.

## Decision Drivers

* One client must not be able to occupy the router on its own.
* A refusal must be immediate and say why, so a client can back off instead of timing out in the queue.
* Existing clients must keep working; the operator sets higher values where a client needs them.

## Considered Options

* Per-client `max_concurrent` (default 4) and `max_body_mib` (default 16), overridable in the configuration and the UI;
  429 with `Retry-After: 1` and 413 at once
* Only optional limits, effective where configured
* Measure first, decide later

## Decision Outcome

Chosen option: "Defaults with per-client overrides", because a default protects every client the operator did not think
of, while the measured heavy users get explicit values. Optional-only limits protect nothing until someone sets them.

* `client_auth.clients.<name>.max_concurrent` (1-256, default 4) counts the client's requests in progress on the API port,
  all routes; the next one gets 429 with `Retry-After: 1` and the limit in the message, before any work. A slot is given
  back when the request finishes, fails or the client disconnects.
* `max_body_mib` (1-64, default 16) refuses a request whose declared `Content-Length` is larger with 413. A chunked body
  without a declared length stays bounded by the app ceiling of 64 MiB.
* The router's own UI identity is not a configured client and is not limited. Refusals are audited (`concurrency_limited`,
  `body_too_large`) and counted per client; `skirnir_client_inflight` and `skirnir_client_peak_inflight` show occupancy.

### Consequences

* Good, because a single client, also with a stolen token, can hold at most its slots.
* Good, because the peak gauge turns the limits into measured values over time.
* Bad, because a client that legitimately runs more in parallel gets 429 until the operator raises its limit.
* Neutral, because cost bounds stay per cloud provider (monthly budget); time bounds stay `request_timeout_s`.

### Confirmation

`test/selftest_client_limits.py`: the fifth concurrent request of a default client gets 429 with `Retry-After` and does no
work, another client has its own slots, a declared 17 MiB body gets 413 unless the client allows 32 MiB, a disconnecting
client gives its slot back, configuration bounds and UI fields, and the gauges in the metrics.
