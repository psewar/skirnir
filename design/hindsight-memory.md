# Optional Hindsight memory (PR 2)

This adapter keeps long-term knowledge separate from raw session history and task
checkpoints. It does not copy sessions, configure Hindsight models, install a Hermes
plugin, or perform compaction. All existing clients stay stateless unless they
explicitly request memory and receive a server-side grant.

## Contract and compatibility

The official REST reference and retain/recall/Hermes guides were checked on 2026-10-18:

- https://hindsight.vectorize.io/api-reference
- https://hindsight.vectorize.io/developer/api/recall
- https://hindsight.vectorize.io/developer/api/retain
- https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs-integrations/hermes.md

The adapter uses these paths relative to the configured service URL:

| Operation | HTTP contract |
| --- | --- |
| Recall | `POST /v1/default/banks/{bank_id}/memories/recall` with `query`, `budget: low`, `max_tokens`, `include: {entities: null, source_facts: {max_tokens: 0}}`; reads `results[].id/text/type/source_fact_ids` |
| Retain | `POST /v1/default/banks/{bank_id}/memories` with `async: true`, a deterministic UUID `operation_id`, and one item containing `content`, `document_id`, `context`, `metadata` |

Use a deployment supporting the documented caller-supplied async `operation_id`.
The response must acknowledge the same ID. There is no fallback to unkeyed or
synchronous retention for older servers. The operation ID prevents duplicate queued
work after a lost acknowledgement; document ID alone would only upsert, potentially
repeating expensive extraction. Treat event IDs as immutable identifiers and always
reuse the same content for retries. The local bounded deduplication cache rejects
conflicting payloads; across restarts the upstream operation is authoritative.

Recall POST is read-only and retryable. Retain retries use the same operation UUID
and payload. Only connection errors/timeouts and HTTP 429/500/502/503/504 are retried,
at most `retries` times (0–2). Other failures, malformed/oversized responses and
redirects are not retried. A missing recall bank (404) returns an empty result without
creating a bank. Skirnir never exposes bank-management endpoints.

Recall sends `include: {entities: null}`. Hindsight returns entity state by default
(documented since 0.7: null skips the entity JOIN), and Skirnir never passes it on.
Measured on a bank of about 90 matching facts at a 4096-token budget, this saves about
14 % of the response (85 KB to 73 KB). The response size is driven by the number of
facts, which `recall_max_tokens` controls. Skirnir passes on at most `recall_max_facts`
(10) facts within `recall_max_bytes`, so a large `recall_max_tokens` mostly adds bytes
that are dropped again and can push the response past `max_response_bytes`.

**Duplicates.** Hindsight returns an observation next to the fact it was derived from,
for example a fact with its appended `| When: … |` context and an observation with the
bare statement. Both used to take a place, so of 10 places often only 6 to 8 carried
different statements. Recall therefore also sends `include.source_facts` with a zero
budget: each observation lists the ids of its source facts (`source_fact_ids`) without
their text (+2.6 % response size, measured). When an observation has exactly one source
fact, the higher-ranked of the pair keeps its place and the other is dropped, counted as
`skirnir_memory_events_total{operation="recall",outcome="duplicate"}` and reported as
`duplicates` on explicit recalls. An observation summarising several facts is more than a
copy of any of them; it and its sources all stay.

**Why 10.** The number came from the first implementation without a recorded reason.
A replay of 27 realistic test questions against one real bank (the router's own filter
over the stored raw Hindsight answers) measured the alternatives. Over the gateway, the
Hermes provider sends `max_tokens: 4096`, which caps the block at 4096 bytes as well:

| Setting | Probe hit | Keyword coverage | Facts | Duplicates | Bytes | Binding limit |
| --- | --- | --- | --- | --- | --- | --- |
| 10 facts, 4 KB, duplicates kept (before) | 25/27 | 79.7 % | 9.5 | 1.1 | 3864 | bytes 13×, count 14× |
| 10 facts, 4 KB, duplicates dropped | 26/27 | 82.2 % | 9.9 | 0.1 | 3733 | count 25× |
| 15–20 facts, 4 KB | 26/27 | 82.2 % | 11.2–11.4 | 0.1 | ≈4040 | bytes |
| 20 facts, 8 KB | 27/27 | 84.9 % | 19.6 | 0.1 | 7285 | count |
| 30 facts, 16 KB | 27/27 | 88.9 % | 30 | 0.1 | 10670 | count |

With duplicates dropped, 10 facts nearly fill 4 KB, so the two limits match. More facts
help only together with a larger byte cap (and, over the gateway, a larger client
`max_tokens`), at about 1.9 to 2.9 times the bytes for 2.7 to 6.7 points of coverage. The
default stays 10; `recall_max_facts` changes it.

## Deployment and opt-in

No additional router dependency is needed beyond aiohttp. Hindsight is an independent
service; the PostgreSQL Session Store is not required for memory and remains optional.

Keep the service private and authenticate it at the service/reverse-proxy boundary.
Use HTTPS with valid certificates. The adapter verifies TLS, rejects URLs containing
credentials/query/fragment, does not follow redirects, use ambient proxies or keep
cookies, and never forwards an inbound client credential to Hindsight.

Provide the following through the service environment or external secret manager:

- `SKIRNIR_HINDSIGHT_URL`: self-hosted Hindsight base URL, optionally with a path prefix.
- `SKIRNIR_HINDSIGHT_TOKEN`: optional server-side Bearer token; required if the service
  is reachable outside an independently authenticated/trusted boundary.
- `SKIRNIR_MEMORY_NAMESPACE_KEY`: stable high-entropy random key of at least 32 bytes.
  Generate privately (for example with `openssl rand -hex 32`); never commit it.

Hindsight runs its own extraction/consolidation LLMs and embedding models. Configure
those separately to use local models when processing confidential content. An HTTPS
URL does not prove that the service's downstream models are local; this adapter cannot
verify or override its deployment's downstream egress. No cloud service is contacted
by default. Enabling the adapter is an operator decision to trust the configured
service and its model/data handling.

```yaml
router:
  hindsight:
    enabled: false                 # opt in after configuring the external service
    endpoint_env: SKIRNIR_HINDSIGHT_URL
    api_key_env: SKIRNIR_HINDSIGHT_TOKEN
    namespace_key_env: SKIRNIR_MEMORY_NAMESPACE_KEY
    timeout_s: 3
    retries: 1
    fail_open: true                # inference only: proceed without facts, state in X-Skirnir-Memory-State
    retention_enabled: false
    recall_max_tokens: 512          # token budget sent to Hindsight
    recall_max_bytes: 4096          # byte cap of the rendered recall wrapper (incl. provenance)
    max_query_bytes: 512
    max_response_bytes: 65536
    max_inflight: 4
    queue_size: 32
    max_content_bytes: 4096
    allow_http_loopback: false      # HTTP only for explicit loopback development
    deny_terms: []                 # optional literal case-insensitive deny list
  client_auth:
    clients:
      example-client:
        # Configure a strong token_sha256 through the existing client-auth setup.
        memory:
          owner: skirnir
          projects:
            example-project:
              read: true
              write: false
              data_class: personal # must be one of the configured data classes
              allow_cloud: false
```

Global defaults are disabled, retention disabled, and per-client owner `client`
with no projects. Grants do not support wildcard bank selection or cross-principal
sharing. Read and write scopes are independent. The bank ID is a keyed HMAC of the
configured namespace, authenticated principal, project and data class. Those names
are not sent as bank identifiers/metadata. Different principals, projects, classes
and namespace keys produce different banks. Changing/losing the namespace key changes
bank identity: back it up with secrets and plan a separate migration before rotation.

Endpoint/key/pool/limit changes require restart; disabling stops calls immediately.
Client/project grants are checked live, again before forwarding an augmented prompt
and before dispatching a queued write. Revoked scopes cannot be recovered using a
saved bank ID, header, routing session ID or response-cache key.

## Inference use

```json
{
  "model": "example:latest",
  "stream": false,
  "messages": [{"role": "user", "content": "What is the project's event format?"}],
  "routing": {
    "memory": {"project": "example-project", "recall": true, "retain": false}
  }
}
```

Supported on native `/api/chat`, OpenAI `/v1/chat/completions`, and Anthropic
`/v1/messages`; generate/embedding endpoints reject memory options. Only the final
plain-text user message supplies a bounded query. Tool-continuation or multimodal
last messages do not implicitly become recall queries. Empty/absent/false memory
options do not activate a service or retain history.

Only granted clients authenticated with Bearer, Basic client-token or x-api-key may
use memory, including in observe mode. Anonymous/IP-only/internal UI identities are
denied. A client with its own Hermes/Hindsight provider keeps `memory.owner: client`
and does not request Skirnir memory; even explicit routing opt-in is denied for that
owner, preventing competing router recall/retention.

Recall inserts a separate **user-role data message** immediately before the last
user message. Existing system/developer messages and tool pairs stay intact. The
wrapper identifies every fact by source ID, marks it untrusted/possibly wrong and
instructs the model not to follow instructions in retrieved text. JSON delimiters are
escaped; memories never become higher-priority instructions. This is a trust boundary,
not a proof that an LLM is immune to prompt injection.

Hindsight's max_tokens can return an oversized top fact. Skirnir therefore independently
bounds the complete rendered wrapper by UTF-8 bytes, a conservative upper bound on its text
tokens. Two settings apply, deliberately separate:

- `recall_max_tokens` (512) is the token budget Skirnir sends to Hindsight for its own selection.
- `recall_max_bytes` (4096) caps the rendered wrapper: notice, fact text, source labels,
  provenance metadata, timestamps and JSON overhead. Provenance alone takes a few hundred bytes,
  so one shared limit of 512 dropped even a single short fact.

A gateway client's `max_tokens` can only lower the byte cap. Skirnir drops whole facts instead
of cutting them, and counts each dropped fact as
`skirnir_memory_events_total{operation="recall",outcome="trimmed"}`. A short or empty recall
caused by the size limit is therefore visible instead of looking like missing knowledge.
Bodies/queries/responses are bounded and HTTP concurrency includes pool-wait time in
the timeout. `max_query_bytes` is deliberately small to avoid a conversation dump and
stay within the upstream query-token cap.

A memory project's `allow_cloud: false` pins inference local; an explicit cloud request
is denied. With explicit cloud permission, the effective data class is the strictest
of the project, client and request classes. Existing cloud/provider/secret policies
run on the **augmented** prompt. Request classification may not be lowered to write
higher-class output into a lower-class bank. Shadow execution and the legacy response
cache are bypassed for memory requests to prevent secondary/unscoped disclosure.

Every recall reports the memory's state as a fact, never as an empty list a client has
to interpret:

| `memory_state` | Meaning |
| --- | --- |
| `available` | Hindsight answered. No items means no matching knowledge. |
| `query_filtered` | The sensitive-data filter kept the query from being sent. |
| `unavailable` / `unavailable:<kind>` | No usable answer. `kind` is a fixed category: `timeout`, `transport`, `protocol` (malformed or above `max_response_bytes`), `upstream_auth`, `upstream_rejected`. |

`fail_open` applies to **inference augmentation only**. With `true`, an unavailable
memory lets normal inference proceed without facts, and the response carries the
header `X-Skirnir-Memory-State` (also the `routing` block's `memory_state`). With `false`,
inference returns 503 before it starts. Explicit recall (`/v1/skirnir/memory/recall` and
the Hindsight gateway) always answers 503 with `memory_state` when the memory is
unavailable. Before, an oversized answer from a large bank arrived as an empty list,
and the Hermes provider reported "No relevant memories found" for a memory it never
reached; through the official SDK it is now an error. Successful explicit recalls carry
`memory_state`, `trimmed` (facts dropped by `recall_max_bytes`) and `duplicates` (results
dropped as repeats of a fact already passed on). Authentication/scope
errors are always denied, not treated as outages. Ordinary requests never call Hindsight.

## Conscious, bounded retention

There are two explicit ways to retain, both requiring global `retention_enabled`
and a project write grant:

1. `POST /v1/skirnir/memory/retain` with client-selected, explicitly confirmed knowledge.
2. `routing.memory.retain: true` plus stable `event_id` to opt into **unverified assistant
   response** retention after a successful inference. Recall and retain can be enabled
   independently; neither defaults to retaining a raw conversation.

```json
{
  "project": "example-project",
  "event_id": "curated-fact-001",
  "content": "The synthetic project uses versioned events.",
  "confirmed": true
}
```

The direct endpoint returns 202 `{operation_id, state: queued}`. This acknowledges
only Skirnir's bounded in-memory queue, not durable storage or successful Hindsight
fact extraction. Deterministic operation IDs support client replay after failures
or restarts. A queue-full condition returns 503; conflicting cached IDs return 409.
Other errors use 400/401/403/404/413/503 without backend error text or secrets.
A rejected request (400/413) keeps its fixed public `error.message` and adds
`error.reason`, the fixed text chosen where the problem was detected (for example
`unsupported gateway retain option: tags`), so a client does not have to guess which
field was refused. All memory API responses use no-store caching.

Response retention is eligible only for a completed stop response with nonempty,
bounded plain text and no known client disconnect. Streams must finish writing their
terminal response. A unary response can finish upstream before the client receives
its bytes; transport delivery is not an application-level acknowledgement. There must
be no tools, tool results,
tool calls, structured-format response, truncation, error, cancelled request or missing
terminal stream event. Streaming, unary, local and cloud adapters share this rule.
Assistant-derived retention is explicitly labelled `confirmed: false` and given an
unverified context; it is not silently promoted to confirmed client knowledge.

A single lifecycle-managed worker submits asynchronous Hindsight operations. It
rechecks grants before each send, bounds queue/cache/content size, and drains for at
most one configured timeout on shutdown before cancellation. Queued work may be lost
on restart or dropped on runtime outage; there is no durable outbox in this PR.
Retries are bounded, never an unbounded background loop. An asynchronous retention
failure does not turn an already completed inference into an error.

Secret patterns, email-like strings, private IPv4 addresses and configured literal
`deny_terms` are rejected before outgoing query/retention and before injecting recalled
text. These are defensive heuristics, **not full PII detection**; do not grant retention
to clients that may emit confidential information without an appropriate policy.
Do not send raw session exports or whole conversation histories as memory items.

No new logs/metrics contain prompts, facts, tokens, DSNs, principals, project/bank IDs
or endpoint names. Metrics use fixed operation/outcome labels:
`skirnir_memory_http_total`, `skirnir_memory_http_seconds`, `skirnir_memory_events_total`.
Hindsight/proxy/database logging and backup policies remain the operator's responsibility.

## Direct recall API and validation

`POST /v1/skirnir/memory/recall` accepts only `project` and `query`; returns
`{items:[{source,text}], trusted:false, memory_state, trimmed}`, or 503 with
`memory_state` when the memory is unavailable. Both APIs reject supplied bank, owner,
tenant, endpoint and other unknown fields. They do not expose session history.

```sh
python test/selftest_memory.py
python test/selftest_sessions.py
python test/check_public_changes.py --base origin/main
python -m ruff check .
python router/router.py --check router/config.example.yaml --pure
```

Tests use synthetic loopback servers to exercise actual REST paths/payloads/auth,
missing banks, retries/redirects/timeouts, malformed/oversized responses, scope
isolation/revocation, all inference protocol shapes, retention eligibility and data
boundaries. They do not contact a real Hindsight service or any personal infrastructure.
A real Hindsight/model deployment smoke test is an operational follow-up using
synthetic data, not permission to import personal memories.

The public-change scanner and AGENTS.md privacy guidance supplement manual review of
every change before publication. A generic scanner cannot detect all personal names
or infer whether apparently ordinary text describes a real deployment.
