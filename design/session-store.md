# Optional persistent sessions (PR 1)

> Decisions (why): [ADR-0027](../docs/adr/0027-optional-session-store-in-postgresql.md)

Skirnir can store **explicit client events** in PostgreSQL. It never records ordinary
inference requests automatically. For separately enabled, explicit `skirnir-managed`
inference, see [managed context](managed-context.md). Default: off, no database or `asyncpg` dependency.
OpenAI, Anthropic, Ollama, streaming and tool-call translation are unchanged.

`routing.session_id` remains an arbitrary **affinity key**, with its existing TTL.
A Session API `id` is a server-generated UUID, scoped to an authenticated client
name. Supplying that UUID in `routing.session_id` does not load or save context.
Client names must remain stable across token rotations; renaming a client creates
a different principal. A session ID is never a credential.

## Enable

1. Use Python 3.11+ and install `pip install -r router/requirements-session.txt`.
   Ordinary installations keep using `router/requirements.txt`.
2. Provide a dedicated PostgreSQL database (tested in CI with PostgreSQL 16;
   pgvector is not needed). The database role must be able to create the dedicated
   `skirnir_sessions` schema and its objects. Keep it private to the router. Use
   PostgreSQL TLS with certificate verification (`sslmode=verify-full`) for remote
   databases and protect credentials through the service's environment/secret manager.
3. Set `SKIRNIR_SESSION_DSN` to the connection URI through the service environment.
   Do not put connection strings in config.yaml, logs, shell history or the repo.
4. Enable the backend and grant specific projects to specific authenticated clients:

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

## Migrations, backups, rollback

`session_migrations.py` contains ordered migrations and a schema-version table.
Startup migrations run under a PostgreSQL transaction/advisory lock, including with
multiple routers starting concurrently. Errors roll back the whole migration. A
schema version newer than the binary is rejected. Do not edit released migrations;
append migrations for compaction versions or additional session modes in later PRs.
The dedicated schema avoids modifications to existing application tables.

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
pip install -r router/requirements-session.txt
# Set SKIRNIR_TEST_SESSION_DSN to a disposable local PostgreSQL database first.
python test/integration_sessions.py
```

The integration test skips without that environment variable; CI supplies a fresh
PostgreSQL 16 service and exercises real transactions, concurrent/idempotent writers,
owner isolation through HTTP, restart persistence, bounded reads, archive/delete,
timeout/cancellation rollback, migration rollback and redacted failures. Tests use
synthetic owners and remove their sessions; never point them at production data.
Managed compaction and memory-outage tests are documented in the related guides.
