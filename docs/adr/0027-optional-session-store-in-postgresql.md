---
status: accepted
date: 2026-10-19
decision-makers: operator
---

# An optional session store in PostgreSQL keeps only explicit client events

> Backfilled on 2026-10-30, decided on 2026-10-19 (v0.14.0, router 0.8.0). Source:
> `design/session-store.md`.

## Context and Problem Statement

Agents want to persist conversations and task checkpoints across restarts and hand them between each other. The router
had no database by design (ADR-0005), and most clients are stateless.

## Decision Drivers

* Normal inference stays stateless and database-free.
* Nothing is recorded that a client did not explicitly send for storage.
* Sessions are scoped to an authenticated client; a session id is never a credential.

## Considered Options

* An optional PostgreSQL store for explicit client events, off by default (`asyncpg` only installed when used), server-
  generated session UUIDs scoped to the client name
* Record ordinary inference requests automatically as sessions
* Leave persistence to each client

## Decision Outcome

Chosen option: "Optional store for explicit events", because it serves the agents that need it without changing anything
for the others. `routing.session_id` stays a mere affinity key with a TTL and grants no access to stored sessions.

### Consequences

* Good, because installations without the feature have no database and no new dependency.
* Bad, because a PostgreSQL instance, migrations and backups are needed once it is enabled.

### Confirmation

Integration tests against a disposable PostgreSQL (`integration_sessions`), run in CI; selftests for scoping and the
default-off state.
