# Shared memory, agent exchange and reliability

> Decisions (why): [ADR-0030](../docs/adr/0030-shared-memory-scopes-and-agent-interoperability.md)

[Deutsche Anleitung](shared-memory-interoperability.de.md). This extends the existing
[session store](session-store.md), [memory adapter](hindsight-memory.md) and
[context engine](managed-context.md); it does not replace them.

## Activation and boundaries

All new switches default off. Existing private banks keep exactly the previous
HMAC identity (`v1`, principal, project, classification). Existing client memory
configuration defaults to `strategy: legacy`: explicit `routing.memory` continues
to work unchanged. No database or Hindsight dependency is added to normal inference.
The Hindsight SDK is a **test** dependency, not a router runtime dependency.

Install `router/requirements-session.txt` only for the PostgreSQL store. Set the
DSN, Hindsight HTTPS endpoint/token and independent random namespace/signing keys
through the service environment or secret store. Never put these values in YAML.
Keep the namespace key stable: changing it changes bank/operation identities.
Rotated keys do not redirect pending operations into a new bank; their binding
check revokes them. Handoff signing-key rotation invalidates outstanding packages.

The following is an illustrative fragment, merged into an existing router config.
`agent-a` and `agent-b` also need normal authenticated client credentials configured
outside this example. IP-only identity never grants session or memory access.

```yaml
router:
  session_store:
    enabled: true
    dsn_env: SKIRNIR_SESSION_DSN
    handoff_enabled: true
    handoff_key_env: SKIRNIR_HANDOFF_KEY
    retention_days: 0
    archived_retention_days: 0
    maintenance_interval_s: 300
  hindsight:
    enabled: true
    retention_enabled: true
    outbox_enabled: true
    gateway_enabled: true
    outbox_attempts: 5
    outbox_backoff_s: 2
    outbox_lease_s: 120
    outbox_capacity: 1024
    outbox_retention_days: 0
    recall_min_score: 0.0
  memory_scopes:
    shared-research: {data_class: personal, allow_cloud: false}
  context_manager:
    enabled: true
    optimize_recall: true
    recall_budget_percent: 20
    reduce_tool_results: true
    tool_result_bytes: 1024
    auto_checkpoints: true
  client_auth:
    clients:
      agent-a:
        session_projects: [example-project]
        memory:
          owner: skirnir
          strategy: hybrid
          default_project: example-project
          projects:
            example-project:
              scope: shared-research
              data_class: personal
              read: true
              write: true
              delete: false
              allow_cloud: false
      agent-b:
        session_projects: [example-project]
        session_handoff_from: [agent-a]
        memory:
          owner: client
          strategy: manual
          projects:
            example-project:
              scope: shared-research
              data_class: personal
              read: true
              write: false
              delete: false
              allow_cloud: false
```

A shared scope is server-owned; clients select only their configured project alias.
Multiple aliases may reference a scope, and a client may have several grants. The
shared HMAC uses `shared-v1`, scope name and classification, independently of the
principal. A missing `scope` keeps a bank private. Shared classification must match
the global scope exactly; cloud use requires both the grant and global scope to
permit it, plus the existing routing cloud checks. Read, write and source deletion
are separate grants; unknown scopes/clients are denied. Policy is checked on each
request and again inside the HTTP concurrency slot before every upstream attempt.
Revocation stops queued work; bytes already sent cannot be recalled. Backend/pool
and lifecycle configuration changes require restart; authorization follows the
router's current loaded configuration after a reload.

**Reading a shared scope next to a private bank (`recall_with`, router 0.8.9).** A
client whose memory plugin serves a single bank (Hermes' Hindsight plugin has one
`bank_id`) cannot reach a second grant through that plugin. A project may name
further projects of the same client that are read with it:

```yaml
projects:
  hermes: {data_class: personal, read: true, write: true, recall_with: [team]}
  team: {scope: shared-research, data_class: personal, read: true, write: true}
```

A recall of `hermes` (gateway or `POST /v1/skirnir/memory/recall`) then also recalls
`team`, at the same time. The facts are interleaved rank by rank and share one set of
limits (`recall_max_bytes`, `recall_max_facts`, the caller's own `max_tokens`); a
statement present in both banks is passed on once. Facts from a further project carry
`project` (REST) or `metadata.memory_project` plus a `[team] ` prefix in the text
(gateway; the Hermes plugin forwards only the text). The answer reports each further
project as `also: [{project, memory_state, items}]`; a further project that is
unavailable, missing or no longer readable adds nothing (`denied` when the grant is
gone) and never turns the private answer into an error, while a failing primary bank
fails the whole recall as before. Each name must be another project of the same
client with `read: true` (max 4); loading refuses anything else. Writing is not
affected: retains go only to the addressed project. Automatic recall for inference
(`routing.memory`, strategies `auto`/`hybrid`) is not affected either.

| Strategy | Behavior |
| --- | --- |
| `legacy` (default) | Existing explicit `routing.memory`; requires router ownership. |
| `off` | No memory access, including explicit gateway/tools. |
| `manual` | Explicit memory REST/gateway only; client ownership is supported. |
| `auto` | Automatic recall for `default_project`; output retention only with explicit `auto_retain: true` and write/retention enabled. |
| `hybrid` | Automatic recall plus explicit REST/tools; no automatic output retention. |

Automatic strategies require `owner: skirnir` and a granted `default_project`.
`owner: client` with `strategy: manual` lets Hermes own memory without a second
router injection. These scoped strategies bypass the shared response cache and
shadow/observation inference, including when a particular request skips recall.
Session and memory project must agree when both are used.

## Hermes and official API compatibility

Configure the official Hermes Hindsight plugin, not direct unrestricted access to
Hindsight. Follow the [official integration guide](https://hindsight.vectorize.io/sdks/integrations/hermes)
for installation (`hermes plugins install hindsight`) and select
`memory.provider: hindsight`. Its separate `hindsight/config.json` can contain:

```json
{
  "mode": "local_external",
  "api_url": "https://router.example.net/v1/skirnir/hindsight",
  "bank_id": "example-project",
  "recall_prefetch_method": "recall",
  "recall_types": ["observation"],
  "auto_recall": true,
  "auto_retain": false,
  "memory_mode": "hybrid"
}
```

Supply the **Skirnir client token** using the plugin's external secret/environment
mechanism (`HINDSIGHT_API_KEY`), never the upstream administrative token. Point
Hermes inference to the normal Skirnir inference URL; only its memory URL uses the
gateway prefix. Set that client's memory to `owner: client`, `strategy: manual`.
Additional banks must also be explicitly granted aliases. Keep the Hindsight
service reachable only by the router (firewall/service authentication); the public
reverse proxy must route this prefix to Skirnir and must not publish a bypass to
Hindsight's bank/admin API. Use HTTPS and preserve client authentication headers.
The backend service token stays server-side. Skirnir cannot enforce grants against
a client that independently possesses unrestricted backend access.

Implemented gateway contracts under `/v1/skirnir/hindsight`:

| Method/path | Meaning |
| --- | --- |
| `GET /version` or `/v1/version` | Versionless capabilities; never advertises full-server/append support. |
| `POST /v1/default/banks/{alias}/memories/recall` | Bounded untrusted text recall with `memory_state`, `trimmed`, `duplicates` and `limited_by`; a client's `max_tokens` counts fact text, as in Hindsight; 503 with `memory_state` when the memory is unavailable (never an empty list). |
| `POST /v1/default/banks/{alias}/memories` | One bounded item with optional `context` and `timestamp`, `async: true` or `false`, persistent outbox required. |
| `GET /v1/default/banks/{alias}/operations/{uuid}` | Owner-scoped operation status; accepted waits for actual upstream completion. |

Native retain returns HTTP **200**, matching the official SDK response contract,
with `durable: true` and an operation UUID. With `async: true` this confirms local
admission only. With `async: false` (the Hindsight default, used by the Hermes
`hindsight_retain` tool in provider 1.0.0) the job is admitted durably the same way and
the answer waits for **verified completion** (see "Verified delivery" below), bounded
by `sync_wait_s` (20 s). A timeout returns **504** with the operation UUID: the job
keeps going, and resending identical content returns the same operation. At most
`sync_max_waiting` synchronous retains wait at once; beyond that the gateway answers
503 without admitting the job. The separate Skirnir `/memory/retain` endpoint returns **202**.
Recall accepts SDK default null expansions, `types` absent, `observation`, or the
standard world/experience/observation set. Server limits cap results and upstream
search uses the low budget. Filters, expanded entities/chunks/source facts, trace,
reflect, bank administration, extraction strategies and append are unsupported
and rejected; retain item `tags`, `entities`, `strategy` and `resolve_entities: false`
are refused with 400 and `error.reason` naming the option. Tags stay refused: the
separation runs over banks and scopes. Provider metadata is not trusted as confirmation.

Retain items may carry Hindsight's `context` and `timestamp`; both reach Hindsight,
so a date need not be written into the text:

- `context` is a label of at most 256 characters without control characters. It
  passes the same sensitive-data filter as the content (`deny_terms`, secrets,
  e-mail and private addresses), at admission and again before delivery. Skirnir
  keeps its fixed notice first and appends the label (`Unverified assistant
  response; not confirmed facts; client context: <label>`), so a client context can
  describe the content but never confirm it; the job stays `confirmed: false`.
- `timestamp` is the event time Hindsight dates the facts by ("When:"). It must be
  an ISO 8601 date and time with a UTC offset (`2026-03-14T09:30:00+01:00`, `Z`
  allowed, seconds and up to six fraction digits optional); impossible dates, a
  missing offset or Hindsight's special value `unset` are refused with 400 and
  `error.reason`. Skirnir passes it on in canonical form (`Z` becomes `+00:00`).
- `null` (as the official SDK sends unset fields) and an empty `context` mean "not
  given": Hindsight then uses its own default (extraction time) and the fixed notice.
- Neither field is part of the operation's identity. Hermes stamps every send with
  the current time, so a replay of identical document/content returns the operation
  admitted first, with that operation's context and event time. Outbox rows store
  both only when given; rows admitted before keep their shape and fingerprint.

Use manual bounded fact retention. Full transcript mirroring is intentionally not
promised: repeated identical provider document/content snapshots deduplicate;
changed snapshots become separate immutable, unconfirmed sources, rather than
implementing stable-document replacement or append. Leave Hermes `auto_retain`
false; oversized transcript retention fails explicitly. A gateway 404 for an
upstream operation evicted from Hindsight follows the plugin's existing polling
behavior; Skirnir's operation API reports `upstream_status: not_found` explicitly.
This is not proof of extraction or erasure.

Contracts were checked against the official
[retain](https://hindsight.vectorize.io/developer/api/retain),
[recall](https://hindsight.vectorize.io/developer/api/recall) and
[document](https://hindsight.vectorize.io/developer/api/documents) APIs, the Hermes
provider blob `d194a893940490bcf0b4004db56f4dcf5e2ea4e1` (plugin 1.2.3), and
OpenAPI blob `54f4e0fd6147825363e9a384ac08bfc493931265` in
[vectorize-io/hindsight](https://github.com/vectorize-io/hindsight).
The optional SDK contract test runs `hindsight-client>=0.10.1,<1` (tested 0.10.3)
against a synthetic loopback server. A real Hindsight extraction/model smoke test
and a full Hermes agent run require an operator-provided test service and are not
part of these CI tests.

## Durable admission, recovery and deletion

`outbox_enabled` requires both Hindsight and the existing PostgreSQL store. With
it disabled, the old bounded in-memory queue remains available and is not durable.
With it enabled, `/v1/skirnir/memory/retain` commits the job before returning 202;
a missing database returns 503. A 202 is not upstream extraction success. Automatic
output capture is best effort: a successful inference response alone never promises
retention; inspect operation status and metrics when retention is required.

Operation UUIDs are stable. Same event/content replays return the existing job;
conflicting content returns 409. Shared operations include the authenticated owner
in their identity. Workers claim using `FOR UPDATE SKIP LOCKED` and expiring UUID
leases, then retry using the same Hindsight `operation_id`. PostgreSQL and upstream
idempotency together handle crash-after-send ambiguity; there is no distributed
transaction or exactly-once transport claim. Admission is capacity-bounded. HTTP
attempts are bounded, leases default to 120 seconds, exponential delay starts at
2 seconds and caps at 3600. After five claims by default the job becomes `dead`.
No retry occurs under changed rights/classification/bank binding: state `revoked`.
Keep authorization and secret configuration consistent across worker instances.

| Operation endpoint (`/v1/skirnir/memory`) | Result |
| --- | --- |
| `GET /operations/{uuid}` | Owner-only state, attempts, timestamps, action and upstream status after acknowledgment; no payload/scope. |
| `DELETE /operations/{uuid}` | Cancel a never-attempted job. Ambiguous or in-flight work returns 409. |
| `POST /operations/{uuid}/delete` with `{}` | Separate `delete` grant; durable selective source deletion. 202 while pending, 200 once deleted. |
| `POST /list` with `{project, type?, query?, limit?, offset?}` | Curators only (see below): one page (1-50, default 20) of the shared scope's memory units with `document_id` (the operation they came from), `source_memory_ids` and provenance. Units the content filter rejects are listed under `withheld` by id only. |

**Curators of a shared scope.** A client with both `read` and `delete` on a project
bound to a shared `scope` curates that scope: it may list it (`POST /list`) and see the
status of and delete any operation in that scope's bank, also other clients' (REST
only; never cancel or retry them, never a private bank). For every other client an
operation stays owner-only and a foreign one answers 404. Each curated deletion is
logged with curator, owner and scope and counted as `outcome="curated"`; the deletion
worker then runs under the curator's grant, so revoking it stops pending deletions.
"Changing" a fact means writing the corrected fact and deleting the old operation;
Hindsight's in-place curation is not exposed.

States are `pending`, `processing`, `accepted`, `completed`, `dead`, `revoked`,
`cancelled`, `delete_pending`, `deleted`. `accepted` means upstream acknowledged
retention and the job is being verified; `completed` means Hindsight reported the
extraction as completed.

**Verified delivery.** Hindsight retries a failed LLM call only briefly (by default
three retries, at most 60 s apart) and then marks the operation `failed`; a local
model busy for hours (a game on the GPU node) therefore loses the extraction. The
outbox checks every accepted job: first after `verify_delay_s`, doubling up to
`verify_max_delay_s`. `completed` ends the job. `failed`/`cancelled` requeues it after
`upstream_backoff_s` (doubling, at most 6 h) under a **new upstream operation id**
with the **same document id**, because Hindsight answers a known operation id with its
old state; at most `upstream_attempts` resubmissions per round, then `dead` with
`upstream_failed`. An operation Hindsight no longer knows becomes `dead`
(`remote_unknown`); one unconfirmed for `verify_timeout_s` becomes `dead`
(`remote_pending`). Each resubmission starts a new send round. A retry round keeps
the resubmission counter, so no failed upstream id is reused. The payload of an
accepted job is kept until it is completed. The gateway status reports an accepted
job whose upstream extraction failed as still `processing`, since it will be resubmitted.

The deletion worker first requires upstream status `completed` (or `failed`, after
which no write can follow), then deletes only its immutable UUID document.
Unknown/pending upstream operations are retried and can become dead letters; they
are not labelled erased. HTTP 404 on the document
DELETE is repeatably absent only after that completion check. Source deletion does
not delete the shared bank or unrelated sources; Hindsight controls reconsolidation
of observations with other sources. There is no guarantee about backups, external
copies, upstream retention or semantically equivalent independently sourced facts.

Session deletion (`DELETE /v1/skirnir/sessions/{id}`) removes that owner's session,
events, compactions, handoff import ledger and scope references, **not memories**.
Session/outbox TTLs default to zero (disabled). `retention_days` applies to sessions
by last update; `archived_retention_days` additionally targets archived sessions.
Maintenance skips live managed turns, processes at most 100 rows per sweep, and
cascades only session-owned data. `outbox_retention_days` scrubs terminal job
payloads; owner/project, opaque IDs, fingerprints, scope bindings and timestamps
remain as an idempotency ledger to prevent replay from resurrecting deleted data.
TTL does not delete the upstream memory. Establish separate backup retention and
upstream lifecycle policies. Do not delete tombstones while replay remains possible.

## Discovery and handoff v1

`GET /v1/skirnir/sessions` lists only the authenticated owner's granted projects.
Filters: `project`, `status=active|archived`, `updated_since` (timezone required),
`q` (project substring only), `limit` (1..100, default 50), `before` (opaque returned
cursor). Results contain minimal identifiers/classification/timestamps, no message
content or metadata. Use `next_before` for the next page. Sorting is descending
`(updated_at,id)`; concurrently updated sessions can move between pages.

Enable handoffs separately with a random signing key of at least 32 bytes.
`GET /v1/skirnir/sessions/{id}/handoff` returns a signed, bounded package:

| Field | Content |
| --- | --- |
| `schema_version`, `kind` | `1`, `skirnir-agent-handoff` |
| `source` | Principal/session/project/classification/mode/last update |
| `context` | Active messages, extractive summary, compaction version, original cursor, latest checkpoint |
| `work` | Open steps, repository/branch/commit/artifact references, observed memory scopes, unresolved tool descriptors |
| `observed` | Last persisted turn status |
| `issued_at`, `expires_at`, `signature` | 24-hour expiry, HMAC-SHA256 authentication |
| `resumable_execution` | Always `false`; no process/tool resurrection |

Session metadata `handoff` accepts repository/branch/commit/artifact references.
Only clean HTTP(S) references or relative/repo references are allowed; no embedded
credentials, query strings, traversal or absolute file paths. No referenced file
is read. Sensitive-content detection can refuse an export; it is a heuristic,
not a universal secret detector. Only export content suitable for its recipient.

`POST /v1/skirnir/sessions/import` accepts
`{"package": <exported object>, "project": "example-project", "event_id": "handoff-1", "mode": "client-managed"}`.
The server validates signature/expiry/source existence/current permissions and
creates a **new owner session** atomically. Cross-client imports additionally need
`session_handoff_from: [source-client]` on the receiving client. Both source and
destination projects must still be granted. Classification cannot be lowered;
mode may be `skirnir-managed` only when enabled. Stable event IDs make import
idempotent, conflicting replay returns 409. Key rotation, source deletion or grant
revocation can invalidate a package. Packages are not encrypted: transfer them over
a trusted authenticated channel and treat them as sensitive session exports.

Source messages, including original system instructions and tool descriptors,
become explicitly untrusted user-role context. They do not execute tools, adopt
system authority, copy memory grants or restore a running agent. Memory scopes
are references only. The latest checkpoint is preserved and available explicitly
through `GET /v1/skirnir/sessions/{id}/checkpoints/latest`.

## Context and knowledge quality

The existing extractive projection and immutable raw events remain the foundation.
`optimize_recall` caps recall to `recall_budget_percent` of the prompt budget and
removes lower-priority recall entries when protected history needs space. Entire
untrusted wrappers are bounded; no wrapper is cut in half. The estimate reuses
`context_check` and accounts conservatively for serialized bytes/tool overhead,
output allowance and safety reserve. It is not an exact model tokenizer.

`reduce_tool_results` selectively reduces **old completed** tool results pinned by
the existing engine, retaining role, ID and pairing plus a source-event marker.
Current tail, systems/developers, checkpoints and unresolved inputs remain exact.
Only projections change; raw events stay immutable. Protected content too large
returns 413, rather than silently truncating or calling an additional model/cloud.
Compaction versions record reduced source sequences and budget/model provenance.

`auto_checkpoints` commits only observed conversation progress atomically with a
completed managed turn; identical checkpoint payloads do not create a new version.
It preserves explicit goals, open work and artifacts, marks pending tools and
external work unverified, and does not copy whole assistant responses, workspaces
or credentials. It cannot infer arbitrary external task completion. Explicit
agent checkpoints remain the way to record real semantic goals and progress.

Memory provenance includes kind (`user_assertion`, `confirmed_information`,
`assistant_derivation`), origin client, observed time, confirmation, version,
source references and optional `supersedes`. Explicit Skirnir retain still requires
`confirmed: true`; automatic/provider retention is always unconfirmed derivation.
Provider metadata cannot promote assistant claims. Version/supersedes are evidence,
not automatic destructive conflict resolution: keep contradictory sources visible
and verify originals. Recall remains untrusted user-role context. Optional
`recall_min_score` uses Hindsight reranker scores; when set above zero, unscored or
below-floor results are excluded. No second knowledge graph is introduced.

## Operations, validation and rollback

Migration **3** appends discovery timestamps/indexes/triggers, handoff import keys,
session scope references and the durable outbox; published migrations 1/2 remain
unchanged. Startup applies it transactionally through the existing migrator.
Take an operator-managed database backup before upgrades. Do not run this test
suite against a real session database.

Control-plane `/admin/context` and the Overview tiles show aggregate backend and
outbox state under existing admin authentication. Hindsight health is `unknown`
until an actual bounded request occurs. Metrics include
`skirnir_memory_http_seconds`, `skirnir_memory_http_total`,
`skirnir_memory_events_total`, `skirnir_context_tokens`,
`skirnir_compaction_saved_tokens`, `skirnir_context_backend_available`,
`skirnir_memory_outbox_jobs`, `skirnir_handoff_total` and cleanup counters.
Labels use fixed operations/outcomes/parts/states, never owner/session/bank IDs or
content. Existing compaction metrics remain available. These scoped requests also
suppress shadow inference and upstream error-body logging.

Troubleshooting: 403 means missing/live-revoked grant; 409 means conflicting event,
lease or ambiguous cancellation; 413 means a bounded context/content limit; 503
means unavailable service/capacity. Inspect aggregate status and the authenticated
owner's operation status, not payload logs. A `dead` job needs operator investigation;
there is intentionally no blind retry endpoint that bypasses rights or idempotency.
Keep dead-letter rows for diagnosis. Restart resumes eligible pending/expired jobs.

Run `python -m ruff check .`, the pure example config check, all `test/selftest_*.py`,
`test/benchmark_context.py` and the existing E2E suite. Install
`test/requirements-hindsight.txt` for the actual SDK contract test. PostgreSQL CI
runs all three integration scripts against a disposable PostgreSQL 16 service.
`benchmark_context.py` uses only synthetic data: budgets 4096/8192/16384, 20 repeated
projections, retained guard markers/tool pairs, deterministic projection hashes and
a switch to a smaller model window. Its fixture drops the conservative estimate
from 83595 to 2495 (97.02%); this measures projection size, not semantic recall
accuracy or real model quality. Timing is informational and host-dependent. Separate
relevance tests reject low-score/unscored facts and check provenance. No real
Hindsight service/model or full Hermes agent is exercised by the synthetic suite.

For rollback, stop new traffic/workers, disable the new options and restore the
previous client memory configuration (older versions reject new config keys).
Do not casually switch durable retention to the volatile queue while jobs remain:
first drain or deliberately suspend them and keep the database. Disable features on the upgraded build to retain the additive schema safely. An
older binary whose migrator knows only versions 1/2 refuses schema version 3: a
code downgrade requires a reviewed compatibility build that recognizes migration 3,
or a separately planned restore of the pre-upgrade database. Never decrement the
version table, edit migrations 1/2 or drop the schema as a shortcut. New signed
handoffs/automatic strategies need the new code. Restore from backup only under an
explicit data recovery plan, including tombstones and upstream idempotency; database
restoration does not roll back Hindsight side effects.
