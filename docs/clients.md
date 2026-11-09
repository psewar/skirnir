# Connecting clients

**English** · [Deutsch](clients.de.md) · [README](../README.md)

Clients talk to the API port **11434**. Each client gets its own identity and token (web UI, tab *Clients*, or
`client_auth.clients` in the configuration, see [configuration.md](configuration.md#client-identities-on-the-inference-port));
the token is shown once. Clients ask for a **role** such as `standard:latest`, not for a model.

| Client | Base URL | Model | Credential |
|---|---|---|---|
| Ollama API (Home Assistant, Node-RED, scripts) | `https://<name>:11434` | role, e.g. `standard:latest` | `Authorization: Bearer <token>` |
| OpenAI-compatible tools and SDKs | `https://<name>:11434/v1` | role | API key = token |
| Anthropic SDKs, Claude Code | `https://<name>:11434` | role, e.g. `code:latest` | `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` = token |
| Clients that cannot send a header | `https://<name>:11434` | role | source IP (`ip:` in the client entry) |

All endpoints: [reference.md](reference.md#interfaces).

## Home Assistant

1. In the web UI, tab *Clients*, create a client `home-assistant` with the roles it may use; copy the token (shown once).
2. In Home Assistant: *Settings → Devices & services → Add integration → Ollama*.
3. URL `https://<name>:11434/`, API key = the client token.
4. Model = the role name, e.g. `standard:latest` (or `auto:latest` with the [auto role](configuration.md#auto-role-the-decision-engine)).

The integration sends the key as `Authorization: Bearer`. Home Assistant must trust the router's certificate (public, or the
private CA added to its trust store). Voice assistants benefit from a role with `priority: interactive` and a `busy_ok` tier,
so they keep answering while a node is gaming. Home Assistant can also show the router as a device with sensors over MQTT
([operations.md](operations.md#home-assistant-entities)).

## Node-RED

- Nodes that let you set headers (e.g. *http request*): POST to `https://<name>:11434/api/chat` with
  `Authorization: Bearer <token>` and a role as `model`.
- `node-red-contrib-ollama` sends its key only to ollama.com, so it cannot authenticate: give its client the Node-RED host's
  source address instead (`ip: ["192.0.2.20"]` in the client entry). A wrong token never falls back to the IP.

## OpenAI-compatible clients and SDKs

```bash
curl https://<name>:11434/v1/chat/completions -H "Authorization: Bearer <client token>" -H "Content-Type: application/json" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}]}'
curl https://<name>:11434/v1/models -H "Authorization: Bearer <client token>"
```

SDKs: base URL `https://<name>:11434/v1`, API key = the client token, model = a role. The router translates `/v1` to Ollama's
`/api/chat` itself, so roles, context tiers and `keep_alive` apply; SSE streaming, `tools` and `response_format` work.
`/v1/embeddings` needs an embedding model on the node.

Native Ollama:

```bash
curl https://<name>:11434/api/chat -H "Authorization: Bearer <client token>" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

## Anthropic clients (Claude Code)

Claude Code and the Anthropic SDKs speak the Messages API. Pointed at the router (`ANTHROPIC_BASE_URL=https://<name>:11434`,
model = a role such as `code:latest`), they get the same as every other client: the role's tiers, the tier's `num_ctx` (or the
larger context the model is already loaded with), warm first, waiting on `gpu_busy`, tool-call rescue, client identity, metrics
and the decision log (`via=anthropic`). Ollama's own `/v1/messages` cannot do that: it knows no `num_ctx`, so the model reloads
with Ollama's default context and pushes out the runner all other clients share.

| Anthropic | Ollama `/api/chat` |
|---|---|
| `system` (string or text blocks, `cache_control` ignored) | one `system` message |
| `text` / `image` (base64) blocks | `content` / `images`; image URLs → 400 |
| `tool_use` (assistant) / `tool_result` (user) | `tool_calls` / one `role: tool` message each, `tool_name` from the matching `tool_use` id; `is_error` → prefix `Error: ` |
| `thinking` / `redacted_thinking` in the history | dropped |
| `role: system` inside `messages` (not in the public API, but Claude Code 2.x sends it) | a `system` message at that position |
| `tools` (`input_schema`) | `tools` as functions; Anthropic server tools without a schema are left out |
| `tool_choice` | `none` drops the tools; `any` / `tool` become a system hint (Ollama cannot force a call) |
| `max_tokens`, `temperature`, `top_p`, `top_k`, `stop_sequences` | `num_predict`, `temperature`, `top_p`, `top_k`, `stop` |
| `thinking: {type: enabled\|adaptive}` | `think: true`; missing or `disabled` → `think: false`; `display: omitted` → thinking blocks without text (with `ping` events while the model thinks) |

Responses carry `id: msg_<request id>` (as in `X-Skirnir-Request-Id`), the requested role as `model`, a `thinking`, a `text` and
one `tool_use` block per call, and `stop_reason` `tool_use`, `max_tokens` or `end_turn` (Ollama does not tell a stop sequence
from a natural end). Streaming follows Anthropic's event order: `message_start`, per block `content_block_start` /
`content_block_delta` / `content_block_stop` (thinking closes with an empty `signature_delta`, a tool call is one
`input_json_delta` with the complete arguments), then `message_delta` with the real token counts and `message_stop`. Ollama sends
its response headers only with the first token, which with a 196k context can take two minutes; after
`router.anthropic.keepalive_s` (15 s) without an answer the router opens the stream and sends `ping` events. From then on a
failing node ends the stream with an `error` event instead of falling over to the next candidate; fast failures come earlier and
keep the fall-over.

Errors use Anthropic's form `{"type": "error", "error": {"type", "message"}}`. No capacity (no node, `gpu_busy`, `vram_full`,
queue deadline) is **HTTP 529 `overloaded_error`** with `Retry-After` where waiting helps, because Claude Code retries 529 by
itself; reason code and blockers are in `error.skirnir`. `count_tokens` returns an estimate (characters per token as in the
context check). Claude Code sends `ANTHROPIC_AUTH_TOKEN` as `Authorization: Bearer` and `ANTHROPIC_API_KEY` as `x-api-key`; the
router accepts both. `anthropic-version` and `anthropic-beta` are accepted and only logged. `router.anthropic.enabled: false`
switches the endpoints off.

`router.anthropic.think` (also in the UI settings, applies without restart): `auto` follows the client's `thinking` block,
`on` and `off` override it. Claude Code asks for adaptive thinking on every turn, and a model that only knows on or off (such
as qwen3.8) then thinks on every turn, including trivial ones like reading a file. The decision log records `think` for every
request.

## The routing block

The client describes, the router decides. Every inference request may carry a `routing` block. Ollama never sees it.

```json
{"model": "standard:latest", "messages": [...],
 "routing": {"require": ["tools"], "prefer": ["thinking"], "min_context": 32768,
             "priority": "interactive", "session_id": "conversation-17",
             "data_class": "internal", "execution": "auto", "request_id": "ha-4711"}}
```

| Field | Effect |
|---|---|
| `require` | Required capabilities (`tools`, `vision`, `thinking`, `structured`, `embedding`, `insert`, `completion`). Tiers without them are dropped; if no tier of the role can, the answer is **400** with a plain-text reason. |
| `prefer` | Wish: if there are tiers with these capabilities, only they are considered, otherwise the wish is ignored. |
| `min_context` | Lower bound for the tier's context. |
| `priority` | `interactive` < `normal` < `batch` for admission; otherwise the role's priority applies, capped by the client maximum. |
| `deadline_ms` | Maximum waiting time in the queue, then 503 with a plain-text reason. |
| `session_id` | Affinity: the same session stays on its warm node (`session_affinity_ttl_s`, 30 min). |
| `data_class`, `execution` | Data class (`personal` < `internal` < `public`) and `auto` / `local` / `cloud` for the [cloud tiers](configuration.md#cloud-as-a-tier-in-roles). |
| `idempotency_key` | or header `Idempotency-Key`: non-streaming repeats return the same response (`X-Skirnir-Idempotent-Replay: 1`). |
| `request_id` | mirrored in response, header and log; otherwise `X-Request-Id` or generated (`skirnir-…`). |

Even without the block the router reads the request: `tools` → tools, images → vision, `think: true` → thinking, `format` →
structured, `suffix` → insert. Capabilities come from Ollama's `/api/show`, complemented by catalogue overrides
(`models.<name>.capabilities: {structured: false}` for models that answer a schema with HTTP 500). Unknown capabilities never block.

The response always carries the headers `X-Skirnir-Request-Id`, `-Node`, `-Model`, `-Tier`, `-Warm`; a request with memory
recall also carries `X-Skirnir-Memory-State` (`available`, `bank_missing`, `query_filtered` or `unavailable[:<kind>]`, see
[design/hindsight-memory.md](../design/hindsight-memory.md)); a request whose answer may be retained carries
`X-Skirnir-Memory-Operation` with the operation id to delete it later (ADR-0076). The `routing` block in the
body (node, model, tier, `reason`, `skipped` with a reason per skipped tier, `candidates` with score, `queued_ms`, `decision`) is
only included if the client itself sent a `routing` block – legacy clients see an unchanged body. For `/v1` it is in the
`chat.completion` or in the last SSE chunk.

Size limits before the backend (413): `max_images` 16, `max_tools` 128, `max_messages` 1000, body 64 MiB. A client's own
`max_body_mib` (default 16) applies before that.

## Memory and sessions

Persistent sessions, long-term memory (`routing.memory`), managed context and shared memory are optional and off by default.
Overview and links to the setup guides: [reference.md](reference.md#optional-persistent-session-api).
