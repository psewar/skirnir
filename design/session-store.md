# Optional persistent sessions (PR 1)

> Decisions (why): [ADR-0027](../docs/adr/0027-optional-session-store-in-postgresql.md) (optional store),
> [ADR-0114](../docs/adr/0114-the-session-store-is-chosen-by-its-connection-url.md) (engine chosen by the URL, SQLite for
> single hosts), [ADR-0122](../docs/adr/0122-the-sqlite-session-store-runs-the-postgresql-statements-on-one-connection.md)
> (how the SQLite store is built)

Skirnir can store **explicit client events** in PostgreSQL or, on a single host, in a SQLite file. It never records
ordinary inference requests automatically. For separately enabled, explicit `skirnir-managed`
inference, see [managed context](managed-context.md). Default: off, no database or `asyncpg` dependency.
OpenAI, Anthropic, Ollama, streaming and tool-call translation are unchanged.

`routing.session_id` remains an arbitrary **affinity key**, with its existing TTL.
A Session API `id` is a server-generated UUID, scoped to an authenticated client
name. Supplying that UUID in `routing.session_id` does not load or save context.
Client names must remain stable across token rotations; renaming a client creates
a different principal. A session ID is never a credential.

## Enable

The store's URL, in the environment variable that `session_store.dsn_env` names, picks the engine
(`session_url.py`, `session_store.open_session_store`):

| URL | Engine | For |
| --- | --- | --- |
| `postgresql://...` or `postgres://...` | PostgreSQL over `asyncpg`, unchanged | existing installations, several routers or a database host |
| `sqlite:///sessions.db` | a SQLite file in the router's state folder (`router.state_dir`) | a single host without a database server |
| `sqlite:////var/lib/x/sessions.db` | a SQLite file at that absolute path (on Windows `sqlite:///C:/x/sessions.db`) | a file outside the state folder |

Any other scheme, a SQLite URL with a query, a fragment, `..` or `:memory:` stops the start with a fixed message that
never contains the URL; `router.py --posture` names it in advance (`session_store_url_unsupported`).

PostgreSQL:

1. Use Python 3.11+ and install `pip install -r router/requirements-session.txt`.
   Ordinary installations keep using `router/requirements.txt`.
2. Provide a dedicated PostgreSQL database (tested in CI with PostgreSQL 16;
   pgvector is not needed). The database role must be able to create the dedicated
   `skirnir_sessions` schema and its objects. Keep it private to the router. Use
   PostgreSQL TLS with certificate verification (`sslmode=verify-full`) for remote
   databases and protect credentials through the service's environment/secret manager.
3. Set `SKIRNIR_SESSION_DSN` to the connection URI through the service environment.
   Do not put connection strings in config.yaml, logs, shell history or the repo.

SQLite (single host):

1. Nothing to install: Python's `sqlite3` with SQLite 3.37 or newer (Debian 12 has 3.40; the start checks it).
2. Set `SKIRNIR_SESSION_DSN=sqlite:///sessions.db` in the service environment. The file is created on first start,
   owner-only (0600); an existing file and its `-wal`/`-shm` files are tightened to 0600 at every start, and a symbolic
   link in place of the file is refused. Its folder must exist and be on a local disk (WAL mode).
3. `min_pool_size`/`max_pool_size` do not apply; `timeout_s` bounds each call and the wait for another process
   holding the file.

Then enable the backend and grant specific projects to specific authenticated clients:

```yaml
router:
  session_store:
    enabled: true
    dsn_env: SKIRNIR_SESSION_DSN
    min_pool_size: 1
    max_pool_size: 5
    timeout_s: 5
  client_auth:
    mode: enforce
    clients:
      hermes:
        token_sha256: "<SHA256 of a strong client token>"
        session_projects: [personal]
        data_class: private
```

Use an existing configured data class; the class is assigned server-side at creation.
`session_projects` defaults to `[]` (denied); `["*"]` explicitly grants every project
**within that principal only**. No cross-principal sharing exists. Project permissions
are checked on every operation, including after reload. There are no memory-bank
permissions in PR 1: future read/write bank scopes are a separate policy boundary.

Pool/backend settings require a router restart. Reloading `enabled: false` immediately
disables access; enabling a previously disabled backend still requires restart. Pool
size: integer 1–50, minimum <= maximum; timeout: finite 0.1–60 seconds. Unknown keys,
invalid types, literal DSN keys and malformed environment variable names are rejected.
Startup checks connectivity and migrates before accepting requests. Explicitly enabled
but broken database/driver/secret configuration fails startup with a redacted message.
At runtime, database errors return 503 only on session operations. Ordinary inference never
calls this store and continues independently of runtime database availability.

Session endpoints require valid **Bearer, Basic client-token or x-api-key credentials**
even when the existing middleware runs in `observe` mode. IP-only identification,
anonymous access and the UI's internal identity cannot read stored conversations.
Use HTTPS on the API listener or at the trusted reverse proxy.

## API contract

Base: `/v1/skirnir/sessions` on the API port. All bodies/responses are JSON unless
noted. Session bodies are limited to **1 MiB**, including chunked requests. Reads use
keyset pagination, `after` (last seen event sequence, default 0) and `limit` (1–100,
default 50). `next_after` is the final returned sequence or the input cursor when
empty. Keep fetching until an empty page; no unbounded history reads exist.

| Method/path relative to base | Body or query | Result |
| --- | --- | --- |
| `POST /` (base, without trailing slash) | `{"project":"personal","metadata":{},"mode":"client-managed"}` | 201 session with owner, UUID, project, mode, data class, status, creation time |
| `GET /{id}` | — | 200 session metadata |
| `PATCH /{id}` | `{"status":"archived","metadata":{"label":"done"}}` | 200; status and/or metadata, metadata replaces previous metadata |
| `DELETE /{id}` | — | 204; permanent deletion of session and all its events |
| `POST /{id}/messages` | `{"event_id":"turn-1-user","message":{"role":"user","content":"Hello"}}` | 201 event, or 200 identical retry |
| `GET /{id}/messages` | `?after=0&limit=50` | 200 `{items:[...],next_after:N}` |
| `POST /{id}/checkpoints` | `{"event_id":"task-1-v1","checkpoint":{"schema_version":1,"goals":["Finish task"],"open_steps":[],"artifacts":["repo:path"],"last_result":"Tests passed","provenance":{"client":"hermes"}}}` | 201 version, or 200 identical retry |
| `GET /{id}/checkpoints` | `?after=0&limit=50` | 200 version history, ordered by event sequence |

Messages retain their original JSON object, including protocol-specific content,
images, tool-call IDs and tool results. Recognized roles: system, developer, user,
assistant, tool, function. The API stores supplied events; it does not claim they
represent a completed or successful inference/tool call. Clients should append only
complete events, and explicitly distinguish failures in their own content.

Event responses contain `event_id`, `kind`, immutable `payload`, `seq`, `version`,
`created_at`, `owner`, `session_id`. Each session has a monotonically increasing
sequence across both kinds. Checkpoint `version` counts checkpoints independently;
message versions are null. Checkpoint schema version is 1; goals/open_steps/artifacts
are optional arrays of strings, last_result an optional string, provenance an optional
object. File references are references only, never automatically read or executed.
Checkpoints describe task progress; they do not restore processes or running tools.

Retries must reuse the same `event_id` (1–128 characters) **and the exact JSON value**.
The ID is unique across both event kinds within a session. A different payload or
kind with that ID returns 409 and leaves the original unchanged. Creates have no
idempotency key; after an ambiguous creation response a retry may create another
empty session. Per-session row locks serialize concurrent writers, archive updates
and deletion; the append transaction commits message/version and sequence together.
An archived session remains readable and permits identical retries, but new events
return 409. Reopen it with `status: active`.

No owner/project reassignment, message update, individual event deletion, implicit
history import, or global session enumeration is provided. Foreign and nonexistent
session IDs both return 404. Invalid bodies/parameters return 400, missing credentials
401, project/client denial 403, oversized bodies 413, backend failure 503. There is no
automatic write retry; a response lost after commit can safely be retried with the
same event ID. Cancellation rolls back open transactions; a commit whose result was
lost is resolved by idempotent retry. Session responses have `Cache-Control: no-store`.

## SQLite engine

`sqlite_store.SqliteSessionStore` provides every method of the `SessionStore` and `OutboxStore` protocols with the
PostgreSQL store's own code (`CoreStore`, `ManagedStore`, `ReliabilityStore`, `ExchangeStore`); only the connection
differs ([ADR-0122](../docs/adr/0122-the-sqlite-session-store-runs-the-postgresql-statements-on-one-connection.md)):

* One `sqlite3` connection, used only by one worker thread, so the event loop never blocks. Every store call holds one
  asyncio lock for its whole block and runs as one `BEGIN IMMEDIATE` transaction: one writer at a time, which is what
  the row locks, advisory locks and `SKIP LOCKED` achieve between PostgreSQL connections. Leases, the single pending
  turn, idempotent appends, outbox claims and exact outbox totals keep their meaning. A second process on the same file
  (a backup, an accidental second router) waits up to `timeout_s` (`busy_timeout`) instead of interleaving.
* Cancellation or a timeout queues the rollback in the worker thread behind the statement that may still run and
  before the next caller's `BEGIN`; the caller gets 503 like on PostgreSQL.
* `sqlite_store.translate` turns each PostgreSQL statement into SQLite once (cached): parameters, schema prefix, casts,
  intervals in microseconds, arrays as JSON lists (`json_each`), `jsonb` operators, row locks and advisory locks.
  Columns PostgreSQL fills with `DEFAULT now()` are set to the transaction's start in the `INSERT`.
* Values are stored as UUID text, JSON text (checked by `json_valid`), integer microseconds (UTC) and 0/1, in STRICT
  tables; the store returns them as asyncpg does (UUID, timezone-aware datetime, dict/list, bool), so API responses
  have the same shape.
* `trusted_schema` off (no trigger or view in the file may call an application function), foreign keys on,
  `secure_delete` on (deleted content is overwritten in the database file instead of staying in free pages), WAL with
  `synchronous=FULL` (a reported commit is on disk). Older page versions stay in the `-wal` file until the next
  checkpoint (automatic after about 4 MiB of writes, and when the router stops), as PostgreSQL keeps them in dead rows
  and WAL segments until vacuum and recycling; backups keep what they copied. Unicode `lower()` is registered for search
  and discovery.
* Substring search compares against the stored JSON text, whose key order and spacing differ slightly from PostgreSQL's
  `jsonb` text; the content itself matches the same way.

## Migrations, backups, rollback

`session_migrations.py` contains ordered migrations and a schema-version table.
Startup migrations run under a PostgreSQL transaction/advisory lock, including with
multiple routers starting concurrently. Errors roll back the whole migration. A
schema version newer than the binary is rejected. Do not edit released migrations;
append migrations for compaction versions or additional session modes in later PRs.
The dedicated schema avoids modifications to existing application tables.

SQLite has its own `sqlite_migrations.py` (migration 1 is the schema PostgreSQL reaches after its migrations 1-6), run
inside `BEGIN IMMEDIATE`, with the same rules: atomic, a newer schema is refused, released migrations stay unchanged. A
later schema change adds a migration to both files.

Back up a SQLite store with `sqlite3 sessions.db ".backup /backup/sessions.db"` (or the router stopped and a copy of the
file); a plain copy of a running store can miss what is still in the `-wal` file. Restore it with the router stopped and
the old `-wal`/`-shm` files removed; the start replays the deletion journal as for PostgreSQL. The file and its backups
hold session content and the memory outbox: protect them like the database. `router.py --posture` reports the file's
place and mode (`session_store_sqlite_file`) and a file others can read (`session_store_file_readable`).

Back up this schema/database before upgrades. Disabling the backend and restarting,
or reverting to the previous router binary, leaves stored data untouched. A downgrade
after a future incompatible migration requires restoring a matching database backup;
there is no automatic destructive down migration. To remove data deliberately, use
session DELETE or have a database administrator remove the dedicated schema after
backup. Deletion is permanent, including checkpoint history; backup retention is the
operator's responsibility. No background deletion/retention job is enabled.

Raw history remains intact except explicit whole-session deletion. Store backups
contain user content and must be protected accordingly. Session API metrics use only
fixed operation and status labels (`skirnir_session_requests_total` and
`skirnir_session_duration_seconds`); no principals, project names, IDs, contents,
DSNs or credentials are added to those labels or database error logs.

## Related optional components

[Hindsight memory](hindsight-memory.md) supplies separately granted recall/retention.
[Managed context](managed-context.md) supplies explicit managed inference, model
budgets, immutable compaction and active-context handoffs. Both remain opt-in.
Hermes continues to manage its own conversation/compaction and memory provider;
explicit storage does not install a Hermes plugin or duplicate that processing.
Raw history, compacted context and external long-term knowledge remain separate.

## Verification

```sh
python test/selftest_sessions.py           # no database/asyncpg needed
python test/integration_sessions.py        # SQLite half always; PostgreSQL half with the variable below
pip install -r router/requirements-session.txt
# Set SKIRNIR_TEST_SESSION_DSN to a disposable local PostgreSQL database first.
python test/integration_sessions.py
```

The contract classes run once per engine (`test/store_engines.py`): SQLite on a fresh file per test in every CI run,
PostgreSQL when the variable is set; CI supplies a fresh PostgreSQL 16 service and exercises real transactions,
concurrent/idempotent writers,
owner isolation through HTTP, restart persistence, bounded reads, archive/delete,
timeout/cancellation rollback, migration rollback and redacted failures. Tests use
synthetic owners and remove their sessions; never point them at production data.
Managed compaction and memory-outage tests are documented in the related guides.
