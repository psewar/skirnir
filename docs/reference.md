# Reference

**English** · [Deutsch](reference.de.md) · [README](../README.md)

## Interfaces

| Port | What | Who |
|---|---|---|
| **11434** | Ollama API: `GET /api/tags`, `/api/ps`, `/api/version`, `POST /api/show`; inference `POST /api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings` (streaming is passed through, `model` in the response carries the role name). `/api/pull`, `push`, `create`, `copy`, `delete` → 403. TLS with the router's own certificate when `api_tls: true` (default `false`). | Clients |
| **11434 `/v1`** | OpenAI-compatible: `GET /v1/models`, `POST /v1/chat/completions` (SSE streaming, `tools`, `response_format`), `/v1/completions`, `/v1/embeddings`. The router translates to `/api/chat` itself so that roles, context tiers and `keep_alive` apply – Ollama's own `/v1` knows neither `num_ctx` nor `keep_alive`. | OpenAI clients |
| **11434 `/v1/messages`** | Anthropic Messages API: `POST /v1/messages` (streaming and not), `POST /v1/messages/count_tokens`; `GET /v1/models` answers in Anthropic form when the request carries `anthropic-version`. Translated to `/api/chat` like `/v1`, see [Anthropic clients](clients.md#anthropic-clients-claude-code). | Claude Code, Anthropic SDKs |
| **11434 `/rerank`, `/info`** | Optional TEI-compatible reranking on a GPU node through the agent tunnel (off by default, client right `rerank`), see [rerank endpoint](#optional-rerank-endpoint). | Memory services |
| **11434 `/v1/skirnir/…`** | Optional session and memory APIs, off by default ([below](#optional-persistent-session-api)). | Agent frameworks |
| **11435** | Web UI (`/`), `GET /admin/state`, `GET/PUT /admin/config`, `POST /admin/try`, `/admin/loadtest`, `/admin/measure`, `/admin/bench`, `/admin/decide`, `GET /admin/decision`, `/admin/usage`, `/admin/ha`, `/admin/nodes` (`POST`/`DELETE /admin/nodes/{fp}/quiet`: quiet one node for a measurement, no unloading or prewarming until a given time, [ADR-0061](adr/0061-quiet-a-single-node-for-a-measurement.md)), `/admin/clients`, `GET /metrics`. Basic Auth (PBKDF2 hashes in the configuration, a verified pair is cached for 10 min). Every non-GET request to `/admin/*` also needs the header `X-Skirnir-UI: 1` (CSRF protection, [ADR-0042](adr/0042-admin-writes-require-a-custom-header.md); the web UI sends it). Default listener `127.0.0.1:11435`; set `control_listen` for remote agents and browsers ([ADR-0040](adr/0040-authentication-fails-closed-control-port-on-loopback-by-default.md)). Without `control_auth.users` the port answers 503, also on loopback; unauthenticated loopback access only with `control_auth.allow_unauthenticated_loopback: true` for development ([ADR-0053](adr/0053-control-port-requires-users-even-on-loopback.md)). TLS with `control_tls`. | Browser, scripts, Prometheus |
| **11435 `/v1/tunnel`** | WebSocket from the agent: login with an Ed25519 signature over a challenge, heartbeat, configuration package, all Ollama calls as multiplexed streams (REQ/RESP/DATA/END/ERR/CANCEL). | Agent → router |
| MQTT 8883 | Discovery and states for Home Assistant (TLS, password from `secrets.env`). | Router → broker |

Requests may take up to 600 s (`request_timeout_s`). If the client disconnects, the router closes the upstream response, the tunnel
sends CANCEL and the agent terminates the Ollama request. The inference port uses TCP keepalive (first probe after 60 s of
silence, then every 15 s, dead after 4): a non-streaming request can run for minutes without a single byte, and connection state
somewhere on the way to the client may expire silently; the probes keep it fresh and make a dead connection show up as an error.

## GPU guard

Agent 0.7.0, [design/gpu-guard.md](../design/gpu-guard.md) (German). The agent sets a power limit of 80 % of the card's default
(460 W for a 575 W card), drops to 70 % for 5 minutes after 10 minutes of sustained full load, re-applies after driver resets,
never raises above the default, and warns on 16-pin voltage sag, memory temperature and hardware throttling. On by default;
opting out (`gpu_guard.enabled: false`) is deliberate and therefore reported as a problem. Setting the limit needs the
LocalSystem account on Windows. The router caps a throttled node to one request, penalises it in the score and forwards
problems to Home Assistant (`modes.gpu_guard`, per-node policy in the agent registry).

## Optional persistent session API

The router can persist explicit client events in PostgreSQL using an opt-in session
API at `/v1/skirnir/sessions`. It is **disabled by default** and adds no dependency
or service requirement to existing installations. Enable `router.session_store`,
install `router/requirements-session.txt` (`install.sh` does), supply the DSN through the configured
environment variable, and grant `session_projects` to individual authenticated clients.

The API supports session metadata/status, append-only messages, idempotent event IDs,
paginated history, versioned task checkpoints and explicit session deletion. Owner
isolation uses the authenticated client, never a supplied session ID. Token credentials
are required even in observe mode; IP-only access is denied. `routing.session_id`
continues to mean node/model affinity only. Existing inference/streaming is unchanged.

`client-managed` keeps explicit event persistence; Hermes retains its own compaction
and memory behavior. See the [API, configuration, migration, rollback and test
guide](../design/session-store.md).

## Optional Hindsight memory

An opt-in Hindsight REST adapter adds scoped long-term recall and conscious asynchronous
retention, independent of the Session Store. It is disabled by default; existing clients
keep their behavior. Service URLs/tokens and a stable namespace key come from the
environment, while client/project policies separately grant read/write access.

Explicit `routing.memory` works through the existing native/OpenAI/Anthropic chat path.
Retrieved facts are bounded, source-labelled untrusted user-role data. Cloud use is
denied by default for memory projects. Retention is separately disabled by default;
incomplete streams, tool workflows and detected sensitive content are excluded.
Hermes clients with their own memory provider keep `memory.owner: client` to prevent
double processing. See [configuration, API contract and limitations](../design/hindsight-memory.md).
Memory states in responses: [troubleshooting.md](troubleshooting.md#memory-states).

Public changes are checked for common identifying/secret patterns in CI, with manual
privacy review required by [repository guidance](../AGENTS.md). Examples/tests use only
synthetic data; connection values and real memory data must stay outside the repository.

## Optional managed session context

Enable `router.context_manager` alongside the Session Store and explicitly create a
`skirnir-managed` session to persist delta-only native/OpenAI/Anthropic chat requests.
Model-dependent budgets, immutable extractive compaction versions, exact system/tool
history and versioned task checkpoints bound the active context. Original events
remain available through paginated history and scoped search. Snapshots export active
context with version/provenance; running processes and tools are not resumed.

Default remains off. Client-managed sessions and stateless forwarding keep their
behavior. Managed inference is currently local only and text/tool only; protected
context that cannot fit fails explicitly. Completed responses are committed before
a normal success response/stream terminal is sent. See [setup, delta/retry contract,
limits and migration/rollback](../design/managed-context.md).

## Optional shared memory and agent interoperability

Server-controlled shared scopes, a durable PostgreSQL memory outbox, signed agent
handoffs and session discovery extend the existing optional context gateway. New
strategies, tool-result reduction, automatic checkpoints and retention remain off
by default. Private bank IDs and existing inference clients stay compatible.
See the [activation, Hermes integration, API and rollback guide](../design/shared-memory-interoperability.md).

Operational improvements: [memory reliability, recovery and live smoke tests](../design/memory-reliability.md).

## Optional rerank endpoint

`POST /rerank` on the inference port speaks the TEI rerank protocol and passes the request through the agent tunnel to a
reranker service on a GPU node (agent route `rerank`, health reported in the heartbeat). Disabled by default; a client needs
the right `rerank: true` and may be identified by source address. No Wake-on-LAN, queue or retry: if no node can rerank, the
router answers at once with `424 rerank_unavailable` and a reason, so a caller with its own CPU reranker falls back without
delay. See [configuration, wire format and reasons](../design/rerank.md). A reranker for Intel iGPU, CPU or NPU ships in
[rerank-server/](../rerank-server/).

## Optional OpenAI-compatible node

A GPU node can run an OpenAI-compatible inference server instead of Ollama, for example Strata, which serves a large
mixture-of-experts model from one GPU plus system RAM. The agent setting `tunnel.upstream_api: openai` (default `ollama`)
points the tunnel at that server: the agent reports its models from `/v1/models` and says so in its facts. The router then
sends chat and generate requests as `/v1/chat/completions` through the tunnel, translated the same way as a cloud request,
so the answer reaches every client format, and it leaves out what only Ollama has: no `/api/show` (capabilities come from
the catalog), no Ollama update. Run the server as an agent child (`children:`) and switch the Ollama child off, because
both need the GPU. The server's GPU memory counts as the agent's own, so busy detection treats it like a loaded model. The
model goes into the catalog with `weights_gib` and its `capabilities`, and into a role tier like any local model. Embeddings
go to `/v1/embeddings` (a server without it is skipped for them), and managed session turns are served as on an Ollama
node. Router 0.10.0, agent 0.21.0.

**Freeing the GPU for a game** (router 0.11.0, agent 0.22.0): such a server cannot unload its model. With
`tunnel.upstream_child` naming the child that runs it, the router stops the server when the node turns busy and starts it
again when the node is free (where Ollama nodes are prewarmed) or when a request needs the model; that request waits until
the model is loaded, up to three minutes. The agent reports the server as *held* or *starting* instead of unreachable, and
its Home Assistant device takes loaded models and GPU memory from that report. Without `upstream_child` a busy node keeps
its model and is only skipped.

**Standby Ollama during a game** (router 0.12.0, agent 0.23.0): while the server is held, the node has nothing to serve,
not even a small `busy_ok` model such as the voice assistant's. With `tunnel.upstream_fallback` the agent starts an Ollama
child in its place once the server has stopped and sends the tunnel's requests there:

```yaml
tunnel:
  upstream: http://127.0.0.1:8080
  upstream_api: openai
  upstream_child: strata
  upstream_fallback:
    child: ollama                      # an enabled Ollama child; the agent keeps it stopped until a hold
    upstream: http://127.0.0.1:11434   # default
    max_ram_percent: 90                # default; 0 = no RAM check before starting the server
```

The agent reports Ollama's models (OLLAMA `serving: "ollama"`) together with the held server's, and the router treats the
node as an Ollama node meanwhile: `busy_ok` models run during the game, anything else is unloaded as usual. A request for
the server's model, or prewarm once the node is free, releases the server; the agent lets running answers on the standby
finish (at most two minutes), stops it, and only then starts the server, so the two never share the card. The standby is
not updated by the router's Ollama update (it is not the agent's managed Ollama).

**Start held, RAM check before a start** (agent 0.26.0, router 0.17.0): the agent remembers the server's models and its
RAM need (peak private bytes of its process tree in the last run) in `<identity dir>/upstream.json`. With that file it
starts with the server held and the standby serving, so a restart does not load the server only for the router to hold
it again; the router releases it when it wants it (a request for its model, prewarm, the window). Without the file
(first start, new server) the server starts once as before and the agent learns both. Before every start the agent
checks that the RAM in use plus that need stays within `max_ram_percent`; otherwise the server stays held and the
OLLAMA frame carries `upstream_blocked` (reason `memory`, available, total, need, limit) until there is room again. The
router then offers no release candidate and answers `memory_pressure` with these numbers; prewarm and the window send
no release meanwhile.

**Switching to the standby for a request** (router 0.15.0, agent 0.25.0): the agent also reports the standby's models
while the standby is stopped (heartbeat `standby_models`, read from its model store `OLLAMA_MODELS/manifests`). A request
whose model only the standby has may then hold the server, as a concrete model or as a role tier: the node is a cold
candidate with the penalty `modes.score.switch` (default 60), so any node that has the model without switching wins.
The request waits until the standby serves, up to three minutes, and then runs as an ordinary Ollama request. Strata
comes back the existing way: with the next request for its model, or through the residency check once the standby's
model has been idle for `residency_idle_s`. The UI marks such models with ↻ in its model lists and models no node can
serve with ✕.
The hold stops the server at once, so since router 0.16.4 a switching request first waits until no answer is running on
the server (an agent's tool calls leave such gaps), at most for its priority's wait budget (`modes.admission`). If no gap
comes, it gets 503 with the reason code `server_busy` and `Retry-After` 30 s.

**Only in a window or on request** (router 0.16.0, `modes.upstream_window`, off by default): such a server takes most of
the card. With the window on, the router keeps it for the hours you set, for example 01:00 to 07:00 for unattended jobs at
night. During the day the standby Ollama serves and is prewarmed like any Ollama node.

- Outside the window, a request for the server's model, for instance through a role of its own, still starts it.
- Outside the window, a server that has been idle for `idle_hold_s` (default 900 s) is held again. Prewarm, residency and
  the end of a game no longer bring it back.
- Inside the window, a held server is released once the node is free and the standby has been idle for the same time, so a
  deliberate standby request at night does not switch back and forth.

## Limits and non-goals

- One router process, not a cluster: shared state (queue, breaker, registry) lives in memory and in JSON files.
- No content classifier for data classes (they are declarations; the opt-in privacy check guards only cloud tiers), no model
  signing beyond Ollama's digests, no OpenTelemetry.
- Busy detection on Windows has no per-process VRAM (WDDM reports `N/A`); it works with totals and learned baselines and has
  hold-off and claim mechanics against phantom values in return.
- `/v1/embeddings` needs an embedding model on the node; chat models answer with 501 there, which the router passes through.
- The deploy scripts (`deploy/deploy.py`) assume LXC; `router/install.sh` covers Debian and Ubuntu.

## Performance

Measured, the router is I/O-bound: around 0.6 ms CPU per forwarded request on the test bench, about 10 ms per `/api/chat` in
production versus 200–300 ms in the model. One process with one event loop is enough because the GPU nodes deliver 2–4
requests/s and the actual limit is `OLLAMA_NUM_PARALLEL`.

## Repo layout

| Path | Content |
|---|---|
| `router/router.py`, `router/skirnir_router/` | Service: `app`, `config` (schema), `proxy` (Ollama API), `openai_api`, `request` (routing block), `scheduler`, `admission`, `nodes`, `poll` (state machine, prewarm), `registry` (agent registry), `tunnel`, `auth`, `cloud`, `decision/` (engines), `metrics`, `perf`, `ops` (idempotency, canary, shadow, manifest), `ha` (MQTT), `admin`, `settings` (UI allowlist), `toolcall_rescue` (tool calls that Ollama's parser loses as text are recovered), `wol`, `posture`, `setup_init` (`--init`) |
| `router/ui.html`, `router/skirnir.png`, `router/favicon.png` | Web UI and logo |
| `router/install.sh` | Install or upgrade the router on Debian/Ubuntu |
| `router/config.minimal.yaml`, `router/config.example.yaml` | Minimal first configuration (template for `--init`) and full reference |
| `router/*.service`, `*.timer`, `*.path`, `render-env.sh`, `render-env.conf.example` | systemd units, secrets renderer |
| `router/requirements.txt`, `router/requirements-session.txt` | Runtime dependencies (the second adds the optional session store) |
| `router/decision-tfidf.json` | trained TF-IDF model of the auto role |
| `agent/` | Agent for the GPU nodes (Go 1.27, Windows service / Linux binary), `Install-Service.ps1`, `Set-UpdateKey.cmd`, `sign.ps1`, own README (German) |
| `decision-eval/` | Dataset generator (136 templates, 1282 examples, group split), trainers for TF-IDF and Jevlike, evaluation (top-1, calibration, chains), results |
| `decision-embed/` | Stage 2 of the auto role: ONNX export, head training, service, container |
| `decision-jevlike/` | Jevlike service (prototype, measured, not in production) |
| `rerank-server/` | Optional cross-encoder reranker with the TEI `/rerank` protocol on OpenVINO (Intel iGPU, CPU, NPU), run as an agent child for a memory service such as Hindsight; own README |
| `deploy/` | `agent_keys.py` (signing keys and signed agent releases, standalone), `deploy.py` and `ops_env.py` (the maintainer's LXC deploy) |
| `design/` | Design notes: `routing-algorithm.md`, `roadmap.md` (stages and decisions), `decision-engine.md`, `gpu-guard.md`, memory and session designs; logos (generated with an image model, metadata removed) |
| `docs/` | User documentation (this folder, English and German), `docs/adr/` (Architecture Decision Records, MADR 4.0, index in `docs/adr/README.md`), `docs/security/` (threat model, risk register, reviews) |
| `test/` | Test suite, fakes, dev environment, test bench |
| `tools/` | measurement scripts for the GPU guard (load test, stage-2 night run) |
| `monitoring/` | Prometheus + Grafana next to the router (compose, scrape config, provisioned dashboard, secret renderer, `deploy_ct.py`), own README |
| `.github/workflows/ci.yml`, `CHANGELOG.md`, `SECURITY.md`, `AGENTS.md` | CI (lint, Go vet/test, Windows cross-build, self-tests; the end-to-end suite nightly), release history, vulnerability reporting, development workflow |
