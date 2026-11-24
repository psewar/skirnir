---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# The SQLite session store runs the PostgreSQL statements on one connection in one worker thread

> Implements the SQLite half of [ADR-0114](0114-the-session-store-is-chosen-by-its-connection-url.md) (proposed in
> pull request #177). Keeps [ADR-0076](0076-deletion-lifecycle-across-stores-and-restores.md) (deletion lifecycle),
> [ADR-0056](0056-owner-only-router-files-and-log-canaries.md) (owner-only router files),
> [ADR-0074](0074-hash-locked-python-installs.md) (hash-locked installs) and
> [ADR-0005](0005-one-python-process-without-database.md) (one router process).

## Context and Problem Statement

ADR-0114 chooses SQLite for single hosts but leaves open how the store is built. The PostgreSQL store is about 40
methods in `session_store.py`, `managed_store.py`, `reliability_store.py` and `exchange_store.py`, each a short
transaction with control flow (leases, idempotency, deletion lifecycle) around PostgreSQL statements: `$n` parameters,
`::type` casts, `clock_timestamp() + $n * interval '1 second'`, `= ANY($n::uuid[])`, `unnest`, `jsonb` operators,
`FOR UPDATE [SKIP LOCKED]`, advisory locks and PL/pgSQL triggers. The same semantics on two engines is the requirement;
two copies of the control flow drift apart.

Python's `sqlite3` is blocking. `aiosqlite` is not among the router's locked dependencies.

## Decision Drivers

* One copy of the store's control flow; a change to a PostgreSQL statement reaches SQLite or fails its tests.
* Same semantics: owner isolation, idempotent appends, one pending turn, leases, exact outbox totals, deletion lifecycle.
* No new dependency (ADR-0074) and nothing on the event loop that blocks.
* The file is private, and deleted content does not linger in its free pages.

## Considered Options

* Reuse the PostgreSQL statements through a fixed, tested rule set; one `sqlite3` connection used only by one worker
  thread; every store call one `BEGIN IMMEDIATE` transaction under one asyncio lock.
* A second implementation of every method with its own SQL.
* An ORM or query builder for both engines.
* `aiosqlite`.

## Decision Outcome

Chosen option: "reuse the statements on one connection in one worker thread", because the control flow stays in one
place and the integration tests show on every pull request that both engines behave the same.

* `sqlite_store.translate` maps a statement once (cached): `$n` to `?n`, the `skirnir_sessions.` prefix away, casts
  away, intervals to microseconds, `= ANY($n)` and `unnest($a,$b)` to `json_each` over JSON lists, `->>` to
  `json_extract`, `jsonb_build_object`/`||` to `json_object`/`json_insert`, `strpos` to `instr`, row locks away. The
  columns PostgreSQL fills with `DEFAULT now()` are added to each `INSERT` as `now()`. An advisory-lock statement is not
  run: the transaction already holds the file's only write lock.
* Values cross the boundary as asyncpg hands them over (UUID, timezone-aware datetime, dict/list, bool); timestamps are
  integer microseconds on a strictly increasing clock, `now()` is the transaction's start like PostgreSQL's.
* One connection, used only by a single worker thread; every call holds an asyncio lock for its block and runs as one
  `BEGIN IMMEDIATE` transaction. Cancellation and timeouts queue the rollback in the same thread, behind the statement
  that may still run and before the next caller's `BEGIN`. `busy_timeout` bounds waiting for another process.
* Own migrations (`sqlite_migrations.py`): STRICT tables, JSON checked by `json_valid`, triggers without application
  functions (touch, exact outbox totals), so the connection keeps `trusted_schema` off.
* The file is created owner-only (0600), an existing file and its WAL files are tightened at every start, a symbolic
  link in its place is refused; `secure_delete` overwrites deleted content in the database file (the `-wal` file keeps
  older pages until the next checkpoint); WAL with `synchronous=FULL`.
* Unicode `lower()` is registered, because SQLite's own folds only ASCII and search and discovery compare in lower case.

### Consequences

* Good, because the store logic exists once, and the four integration test files run on both engines in CI.
* Good, because a single host needs no database server and no extra package; SQLite 3.37 or newer is checked at start.
* Bad, because the rule set must follow new PostgreSQL constructs; a statement it does not cover fails in the SQLite half
  of the tests (STRICT tables catch a value of the wrong type).
* Bad, because the whole store is one writer at a time; fine for one router, not for heavy parallel load.
* Neutral, because substring search compares against stored JSON text, whose key order and spacing differ slightly from
  PostgreSQL's `jsonb` text.

### Confirmation

`test/selftest_sessions.py` checks each translation rule, value conversion, and that both engines provide every
`SessionStore` and `OutboxStore` method. `test/integration_sessions.py`, `integration_context.py`,
`integration_deletion_lifecycle.py` and `integration_interoperability.py` run their contract classes on SQLite in the
CI python job and on PostgreSQL and SQLite in the `sessions-postgres` job; the SQLite classes add owner-only file and WAL
modes, a refused symbolic link, migration rollback and a newer schema, parallel first start, two stores on one file,
deleted content absent from the file, and a file backup restored and replayed.
