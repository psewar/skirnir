# Optional managed session context (PR 3)

`off` remains the inference default. `client-managed` sessions keep explicit event
storage only; Hermes continues to own compaction and its memory provider. The new
`skirnir-managed` mode is an additional, explicit opt-in. Affinity's
`routing.session_id` does not enable it and still grants no access.

## Enable and use

Enable both `router.session_store.enabled` and `router.context_manager.enabled`.
Install `router/requirements-session.txt` and supply the database DSN through the
configured environment variable. Grant `session_projects` only to the appropriate
authenticated clients. No extra LLM, summarizer framework, tokenizer or service is
required. See [Session Store setup](session-store.md) for credentials and TLS.

```yaml
router:
  session_store:
    enabled: true
    dsn_env: SKIRNIR_SESSION_DSN
  context_manager:
    enabled: true
    reserve_tokens: 1024
    safety_tokens: 256
    tail_messages: 8
    summary_bytes: 2048
    max_history_bytes: 2097152
    max_history_messages: 4096
    max_response_bytes: 1048576
    lease_s: 90
```

1. `POST /v1/skirnir/sessions` with
   `{"project":"example-project","mode":"skirnir-managed"}`.
2. Send **only new messages** through the existing Ollama `/api/chat`, OpenAI
   `/v1/chat/completions` or Anthropic `/v1/messages` endpoint. Include:

```json
{
  "model": "example:latest",
  "messages": [{"role":"user","content":"Explain the synthetic example."}],
  "stream": false,
  "routing": {
    "session": {
      "id": "00000000-0000-4000-8000-000000000001",
      "mode": "skirnir-managed",
      "event_id": "turn-1"
    }
  }
}
```

Use the actual UUID returned by creation and existing model/role configuration.
An initial delta may include system/developer messages. Later deltas contain new
user messages or completed tool results, not a client replay of assistant history.
Keep stable event IDs per turn. Input is persisted before inference. Assistant
messages (including thinking and complete tool calls) are persisted only after a
valid completed upstream response. Normal stream completion is held until upstream
EOF and the database commit. Intermediate streamed text can already be visible.
Disconnects and ambiguous transport errors require checking persisted history;
database commit and client receipt cannot be made atomic.

A renewable PostgreSQL lease permits one in-flight turn per session, across router
processes. Concurrent turns return 409. Interrupted turns keep their raw input and
release/expire their lease. Retry the **same** event ID and input: no duplicate raw
messages are appended. Once a newer message was accepted, an older failed turn
cannot be retried. Already completed turns return 409; read the persisted result
instead of running inference twice. This intentionally bypasses the legacy response
cache and shadow/observation inference. A retry after deletion returns 404.

The fingerprint includes messages and inference body options, excluding model,
stream and keep-alive. Use the same options when retrying; a changed payload with
the same event ID returns 409. A fresh event ID starts a new turn. Managed messages
cannot be manually appended through the raw event endpoint. Checkpoints remain
independently writable; updates during inference affect the next turn's projection.
Archive/metadata changes wait until the active turn ends; deletion is always allowed.

## Budgets and immutable versions

Each selected model uses the smaller of its selected context, tier limit and optional
catalog `context_tokens`. The default output allowance is `reserve_tokens`;
positive `options.num_predict` / OpenAI max tokens / Anthropic max tokens reserve
that amount instead. Unlimited/nonpositive native output is replaced by the configured
allowance. The prompt budget subtracts output and `safety_tokens`.

Text is conservatively estimated using serialized UTF-8 bytes plus chat/tool-template
overhead; tool schemas and structured output formats count. This deliberately
underuses many windows and is not a model-specific tokenizer guarantee. Tune reserves
for the deployed templates. Managed vision and template overrides are rejected;
ordinary stateless vision, generation and cloud forwarding are unchanged.

Before forwarding, `extractive-v1` builds a bounded, explicitly **lossy** summary of
older user/assistant text, labelled as untrusted user-role context with source sequence
and original-history lookup guidance. No LLM is called. System/developer messages,
all tool calls/results and a whole recent conversation tail remain exact. Tool pairs
are never separated by compaction. Pending tool calls must receive matching results
before inference continues; tool IDs returned through each API are also persisted.
Native results can identify a call by `tool_call_id`, or an unambiguous `tool_name`.
OpenAI and Anthropic delta-only results preserve their call reference during translation.
Fragmented native tool arguments are rejected rather than stored as corrupt calls.

Inputs of failed turns and the current pending delta remain exact protected context,
even when they fall outside the recent tail. Starting a newer turn does not silently
summarize unresolved input. These conservative pins may themselves require a larger
model or a new session.

Task checkpoints are separate, immutable versions. Store goals, **unresolved work**,
artifact references and last result explicitly through `/checkpoints`; the complete
latest checkpoint is included with every projection and is never summarized.
A text excerpt cannot guarantee preservation of every semantic detail. Use checkpoints
for essential work and search raw originals when exact details matter.

A smaller target window may append another compaction version even at the same source
boundary. Versions record algorithm, source boundary, target model, prompt budget and
previous version. Raw events and previous versions are never rewritten or removed.
Protected content, tail or checkpoint too large returns 413. Database/compaction
failure fails closed (503), preserving raw input. No oversized prompt is silently
forwarded. Working-set and response limits also fail explicitly. If exact tool history
or pins grow too large, export/search the session and start a new explicitly seeded
session. Failed inference can leave an unused compaction version; it remains a valid
projection of the same immutable inputs.

Managed inference is currently **local only**. An explicit cloud request is rejected;
auto fallback is pinned local. Session classification is fixed at creation; requests
or current client policy requiring a stricter class are rejected rather than storing
content in a less restricted session. No downgrade via `routing.data_class` is allowed.

Hindsight remains separately opt-in, with its existing read/write grants and data
policies. When combined, the memory project must equal the session project. Recall
is ephemeral and included in the target budget; it is never copied into raw history
or compaction versions. Automatic output retention retains its existing restrictions.
Session history is not automatically retained as long-term memory.

## Export, search and selective reconstruction

All routes use the same credentials, owner isolation and live project grants as the
Session API and return `Cache-Control: no-store`.

| GET path after `/v1/skirnir/sessions/{id}` | Result |
| --- | --- |
| `/compactions?after=0&limit=50` | Immutable versions; `after` / `next_after` refer to compaction version, limit 1–100 |
| `/snapshot` | Schema version 1, active messages, latest checkpoint and compaction, source/session/class/version provenance, latest turn state |
| `/search?q=synthetic&after=0&limit=50` | Literal case-insensitive substring search in raw event JSON; event-sequence pagination |
| `/messages?after=0&limit=50` | Unmodified paginated originals, including summarized messages |
| `/checkpoints?after=0&limit=50` | Independent checkpoint versions in event-sequence order |

Snapshots are coherent, bounded **active-context exports**, not full history backups.
`raw_history_complete` indicates whether older messages were summarized. Use the
paginated raw event endpoints for a complete export. `last_turn` exposes pending,
failed or completed status; an expired pending lease may still show pending until
another claim marks it failed. No background recovery invents a response.

Handoffs explicitly declare `resumable_execution: false`. Files/artifacts remain
references. Running OS processes, working directories, credentials and tools are
not restored. A client may select raw search results and deliberately seed a new
session (or its own agent context) after applying the destination's data policy.
There is no automatic cross-owner import or arbitrary snapshot execution. Treat
exported content and provenance as untrusted data. Never blindly inject all history.
Search is bounded and SQL-parameterized; large raw datasets may need narrower paging.
Keep access/HTTP query logs disabled or redacted because search terms can be sensitive.

## Migration, rollback and tests

Schema migration 2 appends `turns` and `compactions`, extends the session-mode
constraint and leaves migration 1 unchanged. Startup runs migrations transactionally
under the existing advisory lock. All data cascades on explicit session deletion.
Take a database backup before upgrades. To stop managed inference, disable
`context_manager.enabled`; ordinary inference and explicit session reads remain.
To roll back application code, keep the migrated database and a version that
understands schema 2. Older schema-1 binaries intentionally refuse startup against
a newer database. Restore a pre-upgrade backup into a separate database for a full
binary rollback, or disable the Session Store; do not drop tables or rewrite versions
as part of ordinary rollback. No deployment or merge is performed by this PR.

Run `python test/selftest_context.py`, all existing self-tests, `ruff check .`,
`python router/router.py --check router/config.example.yaml --pure` and
`python test/run_tests.py`. Set `SKIRNIR_TEST_SESSION_DSN` to a **disposable** test
PostgreSQL database for `python test/integration_sessions.py` and
`python test/integration_context.py`. CI supplies PostgreSQL 16. The new database
suite covers leases/concurrency/expiry, retry/rollback, raw/version preservation,
checkpoints, scope/search/cascade, and actual inference/stream/tool handling with
synthetic loopback model responses. No personal infrastructure or real histories
are used. `skirnir_context_events_total` records operation/outcome only, without
session IDs, text or credentials.
