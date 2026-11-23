---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# The session store is chosen by its connection URL; SQLite for single hosts

> Part of the portability roadmap, phase 3 ([design/portability.md](../../design/portability.md)). Extends
> [ADR-0027](0027-optional-session-store-in-postgresql.md) (optional session store in PostgreSQL) and keeps
> [ADR-0076](0076-deletion-lifecycle-across-stores-and-restores.md) (deletion lifecycle) and
> [ADR-0005](0005-one-python-process-without-database.md) (the router itself needs no database). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

The optional session store exists only for PostgreSQL. The boundary is already drawn: `SessionStore` is a `Protocol` in
`router/skirnir_router/session_store.py` (line 25). But there is no factory: `session_api.py` (around line 43) calls
`PostgresSessionStore.open(dsn, cfg)` directly with the DSN from the environment variable named by
`session_store.dsn_env` (`router/skirnir_router/session_config.py`, line 15), and `PostgresSessionStore` (line 62)
combines the session, reliability and exchange stores in one class over `asyncpg`.

The SQL is engine-specific: `jsonb` columns, PL/pgSQL trigger functions (`touch_session`, `count_outbox`) and
`pg_advisory_xact_lock` in `router/skirnir_router/session_migrations.py` (around lines 67 and 115-118), and
`FOR UPDATE SKIP LOCKED` for leases in `router/skirnir_router/reliability_store.py` (lines 43, 48, 177).

The reliable memory outbox lives in the same store: with `outbox_enabled`, `memory.py` (lines 487-491) takes the session
store as the outbox and refuses to start without one. So reliable long-term memory requires PostgreSQL too.

A single-host user who wants managed sessions or a reliable memory outbox has to run a PostgreSQL server for a few
thousand rows.

## Decision Drivers

* Sessions and a reliable outbox on one host without a database server.
* Same semantics on every engine: owner isolation, idempotent appends, leases, deletion journal replay (ADR-0076).
* No change for existing PostgreSQL installations.
* No new code path chosen by anything but the configuration (ADR-0118).

## Considered Options

* A factory `open_session_store(url)` that picks the engine from the URL scheme; PostgreSQL and SQLite; the outbox as its
  own `OutboxStore` protocol.
* PostgreSQL only (status quo); document a small PostgreSQL container for single hosts.
* An ORM or query builder over several engines.
* MariaDB/MySQL as a second engine.

## Decision Outcome

Chosen option: "A factory by URL scheme, PostgreSQL and SQLite, and a separate `OutboxStore`", because it serves single
hosts with a file and leaves PostgreSQL installations untouched.

* `open_session_store(url)`: `postgresql://` (or `postgres://`) opens today's `PostgresSessionStore`; `sqlite:///path`
  opens a SQLite store. An unknown scheme is a configuration `Problem`. The DSN keeps coming from the environment
  variable named by `dsn_env`.
* Migrations per engine. SQLite uses `BEGIN IMMEDIATE` transactions for what PostgreSQL does with advisory locks and
  `SKIP LOCKED`; there is one router process (ADR-0005), so a single writer is enough.
* `OutboxStore` becomes its own protocol, so the memory outbox can use the session store or a store of its own (SQLite or
  an append-only journal) and reliable memory no longer requires PostgreSQL.
* MariaDB/MySQL only when someone asks for it with a concrete need.

### Consequences

* Good, because single-host users get sessions and a reliable outbox from a file in `router.state_dir`.
* Good, because the outbox stops being tied to the session store.
* Bad, because every store test runs against two engines, and engine-specific SQL has to stay in step.
* Bad, because SQLite is a single-writer store; it fits one router, not a cluster (which Skirnir does not have).
* Bad, because a SQLite file in the state folder becomes part of what the operator backs up.

### Confirmation

The store self-tests and integration tests (`test/selftest_sessions.py`, `test/integration_sessions.py`, the outbox and
deletion tests) run against both engines in CI; PostgreSQL through `SKIRNIR_TEST_SESSION_DSN` as today. A test checks
that an unknown URL scheme is refused at configuration load and that an existing `postgresql://` DSN opens the same store
as before.
