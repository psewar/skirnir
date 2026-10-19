# Memory reliability and quality

[Deutsch](memory-reliability.de.md). Extends the [shared-memory guide](shared-memory-interoperability.md).

## Optional limits and recovery

New defaults preserve previous behavior:

```yaml
router:
  hindsight:
    outbox_workers: 1
    outbox_client_capacity: 0
  context_manager:
    tool_result_strategy: prefix
```

Set `outbox_client_capacity` to a positive number smaller than `outbox_capacity`
to bound each authenticated owner's active jobs across all projects. This limits
one owner's occupancy; it is not a strict round-robin scheduler or a reservation
for every possible client. Global admission still applies. Admission and retries
share a PostgreSQL transaction/advisory lock, so concurrent submissions cannot
exceed the limits. Duplicate submissions do not consume another slot.

`outbox_workers` accepts 1..16. HTTP concurrency remains bounded by `max_inflight`;
each worker has a database lease and the existing upstream operation UUID. Shutdown
cancels and joins worker tasks; interrupted leases recover after expiry. Configure
the same settings/rights on all router instances. More workers do not promise
exactly-once transport; upstream operation-id idempotency remains necessary.

Owner-authenticated `POST /v1/skirnir/memory/operations/{uuid}/retry` accepts only:

```json
{"retry_id": "synthetic-recovery-1"}
```

It requeues a **dead** job under the same UUID/content, after live bank binding,
policy and capacity checks. Retention retries require current write permission,
enabled retention and content still allowed by the sensitive-data filter. Deletion
retries require the separate delete grant and still wait for upstream completion.
Cancelled, revoked and deleted jobs cannot be resurrected. Payloads already purged
by retention cannot be retained again through this endpoint. At most three explicit
recovery rounds are allowed; each round uses the configured attempt limit/backoff.

Use a stable retry ID for the same recovery action. All three IDs remain in a
bounded replay ledger; even an older retry request replayed after a newer round
returns the current state without opening another round. Response 202 reports
local durable state only. Total attempts are never reset: retry/deletion does not
make previously sent work eligible for an "unsent" cancellation. Distinct user
intent needs a new event, rather than deleting idempotency records.

Status adds `last_error` (a fixed safe category), `retry_rounds` and `upstream_attempts`
(resubmissions after a failed upstream extraction, see verified delivery in the
shared-memory guide). Categories are `unavailable`, `timeout`, `transport`, `upstream_auth`,
`upstream_rejected`, `protocol`, `remote_pending`, `policy_changed`, `lease_expired`,
`upstream_failed`, `remote_unknown`. No upstream body,
exception text, endpoint or credential is persisted as a diagnostic. HTTP errors
remain bounded/retried; these categories do not promise the upstream did no work.

## Ledger growth and monitoring

Migration **4** adds retry/diagnostic fields, partial indexes and an aggregate
`outbox_totals` table. Migrations 1..3 remain byte-for-byte unchanged. Existing job
counts are backfilled once; transactional triggers update totals on state changes
and deletes, including rollback. A short advisory lock serializes counter changes
in consistent order. Direct SQL maintenance must not disable these triggers or
truncate the job table independently of its aggregates.

Admin status and metrics read at most eight aggregate rows rather than grouping
the complete tombstone history. The oldest active operation uses a partial index,
as do owner quota checks and terminal payload cleanup. Background status refreshes
every ten seconds; `/admin/context` returns the cached result and `checked_at`.
Repeated browser refreshes no longer trigger additional database probes.
`skirnir_memory_outbox_oldest_seconds` reports the age since creation of the oldest
active job; it is not extraction latency or time since the latest retry.

Terminal payload TTL now also compacts the stored scope to its immutable bank
binding. Owner/project, operation UUID, fingerprint, retry IDs and timestamps stay
available for authorization, status, selective deletion and replay prevention.
The ledger still grows: automatic ID expiration would let old requests resurrect
knowledge. Plan storage/backup retention accordingly. Compacted dead payloads
cannot be retried, but an authorized failed source deletion can still be retried.

Feature rollback: restore previous config values on this build. Older binaries
reject schema version 4; a code downgrade needs a reviewed compatibility build or
a planned database restore. Do not edit released migrations or decrement the
version table. Database restore does not undo upstream Hindsight effects.

## Provenance and context quality

Recall provenance now validates source references individually and emits complete
JSON arrays of at most 16 references, each at most 256 characters. It never cuts
JSON mid-reference. Malformed lists and unsupported object metadata are omitted;
whole candidates must still fit the existing recall wrapper budget. Extra metadata
therefore does not silently exceed prompt limits. Retrieved evidence remains
untrusted even when a source says it is confirmed.

With `reduce_tool_results: true`, optionally set `tool_result_strategy: head_tail`.
Older completed tool results preserve the beginning and end of their byte allowance,
with explicit omission/source markers. The default `prefix` is unchanged. Protected
instructions, unresolved inputs, current tail and checkpoints remain exact; raw
originals remain immutable. Repeated compaction with the same reduction settings
preserves the existing reduced result rather than repeatedly discarding its tail.
A change of strategy cannot reconstruct text already omitted in an older projection;
consult original events for that. This remains lossy context, not semantic certainty.

Synthetic quality tests check explicit decisions, final error messages, open work,
tool pairs and raw-event immutability across repeated compaction/model-window changes.
The existing benchmark still measures size/determinism, not model comprehension.

Hermes provider compatibility additionally accepts its default
`observation_scopes: shared` and preserves that option in the durable payload and
upstream item. Other extraction/scope controls remain rejected. Existing operations
without that option keep their previous fingerprints; changing an existing event's
extraction option is a conflict, not an implicit overwrite.

## Real-service smoke tests

Regular CI uses synthetic HTTP servers and PostgreSQL 16. The live smoke file
`test/integration_live_memory.py` is explicitly skipped unless
`SKIRNIR_RUN_LIVE_MEMORY_TEST=1`. No successful real-service validation is claimed
for those skips. An explicit opt-in with missing configuration fails.

The separate **Opt-in real Hindsight memory smoke** workflow is manual only. Supply
an official Hindsight image tag/digest, extraction provider/model and the repository
secret `SKIRNIR_LIVE_TEST_LLM_KEY`. Prefer an immutable digest for reproducibility.
It starts disposable PostgreSQL plus a loopback-only Hindsight container and tests
real SDK admission, extraction completion, cross-client recall, unauthorized denial
and selective source deletion/document absence. The image may download embedding
models; synthetic extraction can incur model-provider charges. No services, model
calls or secrets are needed by ordinary PR CI. Container/model logs are not uploaded;
the isolated container/volumes are destroyed afterward.

For an operator-run isolated environment, install router session and test Hindsight
requirements, supply `SKIRNIR_TEST_SESSION_DSN`, `SKIRNIR_TEST_HINDSIGHT_URL` and optional
`SKIRNIR_TEST_HINDSIGHT_TOKEN` externally, then enable the flag and run the file.
Only loopback Hindsight URLs are accepted. Use a disposable database, never actual
session data. Each run uses a random bank namespace and synthetic knowledge.

To exercise the **actual official Hermes provider**, run the same script in the
installed Hermes environment and set `SKIRNIR_TEST_HERMES_PLUGIN` to its installed
Hindsight plugin directory. The test imports that provider with its real Hermes
runtime/SDK and exercises explicit retain/recall tools over the ephemeral Skirnir
gateway; only configuration is replaced, not HTTP or SDK behavior. Use a trusted,
pinned provider version. Without the plugin directory this test reports a separate
skip. The manual container CI runs the SDK scenario and reports that Hermes skip;
it does not install an entire agent environment or run a full model-driven agent.

Both live scenarios wait for actual extraction before cleanup. Cleanup targets only
run-owned UUID documents, not the shared bank. A cleanup failure fails the test;
check the disposable service before reusing it. Real model extraction is stochastic,
so a missing marker is a real test failure, not silently converted to success.
Without isolated backend/model credentials, real extraction and Hermes-provider
smoke tests remain **not performed**; mock tests cannot replace that evidence.
