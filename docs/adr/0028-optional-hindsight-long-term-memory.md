---
status: accepted
date: 2026-10-19
decision-makers: operator
---

# Long-term memory is an optional Hindsight adapter with server-side grants

> Recorded retroactively on 2026-10-30; decided on 2026-10-19 (v0.14.0); failures reported as a state since v0.14.1;
> health probed since v0.20.1. Source: `design/hindsight-memory.md`, CHANGELOG.

## Context and Problem Statement

Agents benefit from long-term knowledge (facts learned across sessions), separate from raw session history. A memory
service (Hindsight) exists; the question is how the router uses it without making every client stateful or leaking memory
between clients.

## Decision Drivers

* Clients stay stateless unless they ask for memory and the server grants it.
* Retention must not run expensive extraction twice after a lost acknowledgement.
* A memory failure must be visible, not look like "no knowledge".

## Considered Options

* An adapter to the Hindsight REST API: recall before inference and conscious, bounded retention, both only with a
  server-side grant per client and bank; asynchronous retain with a deterministic operation id
* Copy sessions into the memory service automatically
* Leave memory to each client's own integration

## Decision Outcome

Chosen option: "An adapter with server-side grants", because the router can enforce which client reads and writes which
bank. Retains use a caller-supplied operation id, with no fallback to unkeyed or synchronous retention, so a retry never
queues duplicate work. Recall failures are reported as an explicit memory state (v0.14.1), and the service's health is
probed, not inferred from use (v0.20.1, ADR-0022).

### Consequences

* Good, because memory is opt-in per client and bank, and its state is visible in responses and the journal.
* Bad, because it requires a Hindsight deployment that supports caller-supplied async operation ids.

### Confirmation

Selftests for grants, operation ids and memory states; integration test `integration_live_memory` against a real service
where configured.
