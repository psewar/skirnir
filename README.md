# Skirnir – a role- and state-aware router for Ollama

**English** · [Deutsch](README.de.md)

<img src="design/skirnir-logo.png" alt="Skirnir" width="120" align="right">

Skirnir sits in front of one or more Ollama installations on GPU machines and looks to its clients like **a single** Ollama
server. Clients do not ask for a specific model but for a **role** (`standard:latest`, `gross:latest`, `code:latest` …; role
names are yours to choose, the example configuration uses German ones, `gross` = large). For each request the router picks
node, model and context size based on what is loaded right now, whether the GPU is occupied by a game, how much VRAM is free,
and how fast and how error-free a model was on a node recently. The Ollama API stays unchanged, plus there is an
OpenAI-compatible interface under `/v1`.

Built for a homelab: Home Assistant, Node-RED and local agent frameworks as clients, Windows gaming PCs as GPU nodes that are
not running around the clock and do not belong to the AI alone. The name: in Norse mythology Skirnir is Freyr's messenger, who
rides into foreign realms, negotiates there and returns with the answer.

**Status:** router 0.1.6, agent 0.8.4 (see [releases](https://github.com/psewar/skirnir/releases)), in continuous operation in
one homelab since September 2026, one developer. No
stability guarantees; this README exists in English and German, the sub-READMEs (agent, decision engine) are in German. If you
want to run it, you should know Python, systemd and Ollama.

## What Skirnir does

- **Roles instead of model names.** A role is a ranked list of tiers (`model @ num_ctx`, optionally `busy_ok`). The router takes
  the best tier a node can serve right now; an already loaded model wins ("warm first").
- **Multiple GPU nodes**, Windows or Linux, connected through a small **agent** (Go) that keeps an outbound tunnel to the router.
  No open port on the node, no certificate, no fixed IP. Wake-on-LAN for sleeping nodes.
- **Busy detection.** If a game occupies the GPU (foreign VRAM, utilisation), only `busy_ok` tiers run there; large models are
  unloaded and prewarmed again after the game.
- **Scheduler** with a score (warm, load, VRAM, speed, error rate), a circuit breaker per node and **admission control** with
  priorities and deadlines in the router instead of in Ollama's own queue.
- **The client describes, the router decides:** an optional `routing` block in the request names required capabilities
  (`tools`, `vision`, `thinking`, `structured` …), minimum context, priority, session, data class.
- **Auto role** `auto:latest`: a chain of local decision engines (embedding model on CPU, small LLM, TF-IDF, rules) picks the role
  from the text of the request. Measured 93 to 98 % accuracy.
- **Cloud as a tier.** OpenAI and Anthropic models can sit as the last (or first) tier of a role, with data classes, credential
  scan, monthly budget in CHF and an egress allowlist. By default nothing goes to the cloud.
- **Client identities** on the inference port (Bearer, Basic, source IP), role and model locks, rate limit, audit log.
- **Observable:** Prometheus endpoint, usage per day and client, decision log, Home Assistant device via MQTT discovery, web UI
  with live charts, catalogue, role editor, playground and load test.
- **Operations-ready:** config schema with suggestions on typos, idempotency key, canary and shadow tiers per role, deploy manifest
  with SHA-256, Ollama versions and model digests per node.
- **TLS everywhere**, one process, one event loop, no database. Dependencies: `aiohttp`, `pyyaml`, `cryptography`, optionally
  `paho-mqtt` and `uvloop`.

## Architecture

```
  Clients: Home Assistant · Node-RED · OpenAI-compatible tools · agent frameworks
      │  https :11434   Ollama API (/api/*) and OpenAI API (/v1/*), client identity
      ▼
 ┌────────────────────────── Skirnir (Debian container, Python 3.11, aiohttp) ─────────────────────────┐
 │  Roles & tiers · Scheduler · Admission · Decision engine · Cloud tiers · Metrics · Usage · Audit    │
 │  Web UI and /admin/*  https :11435 (Basic Auth)              MQTT discovery ─────▶ Home Assistant   │
 └───────────────▲──────────────────────────────────▲──────────────────────────────────────────────────┘
                 │ wss /v1/tunnel (agent → router,   │ https
                 │ Ed25519 key, multiplexed)         │
   ┌─────────────┴──────────────┐         ┌──────────┴───────────┐
   │ GPU node (Windows/Linux)   │  …      │ Cloud providers      │
   │ Agent (Go) ◀▶ Ollama       │         │ OpenAI · Anthropic   │
   └────────────────────────────┘         └──────────────────────┘
```

The router polls every node every 5 s (`/api/tags`, `/api/ps`); the agent reports GPU utilisation and VRAM every 2 s. From both
the router derives the per-node state `offline` / `free` / `busy`. All Ollama calls made by the router go through the agent's
tunnel; Ollama itself stays on `localhost`.

## Interfaces

| Port | What | Who |
|---|---|---|
| **11434** | Ollama API: `GET /api/tags`, `/api/ps`, `/api/version`, `POST /api/show`; inference `POST /api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings` (streaming is passed through, `model` in the response carries the role name). `/api/pull`, `push`, `create`, `copy`, `delete` → 403. TLS with the router's own certificate (`api_tls`). | Clients |
| **11434 `/v1`** | OpenAI-compatible: `GET /v1/models`, `POST /v1/chat/completions` (SSE streaming, `tools`, `response_format`), `/v1/completions`, `/v1/embeddings`. The router translates to `/api/chat` itself so that roles, context tiers and `keep_alive` apply – Ollama's own `/v1` knows neither `num_ctx` nor `keep_alive`. | OpenAI clients |
| **11435** | Web UI (`/`), `GET /admin/state`, `GET/PUT /admin/config`, `POST /admin/try`, `/admin/loadtest`, `/admin/measure`, `/admin/bench`, `/admin/decide`, `GET /admin/decision`, `/admin/usage`, `/admin/ha`, `/admin/nodes`, `/admin/clients`, `GET /metrics`. Basic Auth (PBKDF2 hashes in the configuration, a verified pair is cached for 10 min). | Browser, scripts, Prometheus |
| **11435 `/v1/tunnel`** | WebSocket from the agent: login with an Ed25519 signature over a challenge, heartbeat, configuration package, all Ollama calls as multiplexed streams (REQ/RESP/DATA/END/ERR/CANCEL). | Agent → router |
| MQTT 8883 | Discovery and states for Home Assistant (TLS, password from `secrets.env`). | Router → broker |

Requests may take up to 600 s (`request_timeout_s`). If the client disconnects, the router closes the upstream response, the tunnel
sends CANCEL and the agent terminates the Ollama request.

## Roles and tiers

```yaml
roles:
  standard:
    exposed_as: standard:latest
    priority: normal                      # interactive | normal | batch (admission)
    tiers:
      - { model: qwen3.6:35b-a3b, num_ctx: 65536 }
      - { model: qwen3.6:35b-a3b, num_ctx: 32768 }
      - { model: gemma4:12b,      num_ctx: 65536 }            # fallback tier on the second node
      - { model: granite4.2:8b,   num_ctx: 32768, busy_ok: true }   # may run on a GPU that is gaming
  assist:
    priority: interactive
    tiers: [...]
```

- **Ranking = quality ceiling and cold-start order.** If a listed model is already loaded on any node, it wins (among several warm
  ones, the highest ranked). Globally `modes.warm_first`, per role `latency_first`.
- **Context ladder.** The same model with a smaller `num_ctx` is the next tier when the context does not fit into VRAM.
  Fit check: `VRAM need = weights + context cost × num_ctx/1000 + 0.8 GiB`. Weights and context cost per model live in the
  catalogue (`models:`), measured with the UI button **Measure** (loads at 8k and 32k, reads `/api/ps`) or estimated.
- **Busy.** A node is `busy` when foreign VRAM (used − Ollama − desktop baseline) exceeds the threshold or the GPU is loaded
  without router requests. Then only `busy_ok` tiers run; non-`busy_ok` models are unloaded every 30 s. The desktop baseline is
  learned per node (median of hourly means while the GPU is quiet) and capped by a policy value.
- **Prewarm.** After coming online and after `busy → free` the router preloads the rank-1 models of the roles, as many as fit
  into VRAM together. A **residency rule** brings back an evicted rank-1 model once the evictor has not been requested for
  5 min – otherwise "warm first" would stay stuck on the fallback tier forever.
- **Concrete models** (`qwen3-coder:30b`) remain directly callable (`expose_concrete_models`) and go 1:1 to a node that has
  them; otherwise 404 as with Ollama.
- **Canary** (`roles.<r>.canary: {model, num_ctx, percent}`) sends a share of the traffic to a candidate, even cold.
  **Shadow** (`roles.<r>.shadow`) repeats a share of the requests after the response, without streaming, against a second model on
  a free node; the client notices nothing, speed and error rate end up in the statistics.

Roles, catalogue and most runtime settings are editable in the UI; the UI writes an override file `roles.yaml` next to
`config.yaml` that survives deploys. Ports, TLS, Basic Auth, identities, provider endpoints and the egress allowlist remain
deploy matters on purpose.

## The client describes, the router decides

Every inference request may carry a `routing` block. Ollama never sees it.

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
| `data_class`, `execution` | Data class (`personal` < `internal` < `public`) and `auto` / `local` / `cloud` for the cloud tiers (below). |
| `idempotency_key` | or header `Idempotency-Key`: non-streaming repeats return the same response (`X-Skirnir-Idempotent-Replay: 1`). |
| `request_id` | mirrored in response, header and log; otherwise `X-Request-Id` or generated (`skirnir-…`). |

Even without the block the router reads the request: `tools` → tools, images → vision, `think: true` → thinking, `format` →
structured, `suffix` → insert. Capabilities come from Ollama's `/api/show`, complemented by catalogue overrides
(`models.<name>.capabilities: {structured: false}` for models that answer a schema with HTTP 500). Unknown capabilities never block.

The response always carries the headers `X-Skirnir-Request-Id`, `-Node`, `-Model`, `-Tier`, `-Warm`. The `routing` block in the
body (node, model, tier, `reason`, `skipped` with a reason per skipped tier, `candidates` with score, `queued_ms`, `decision`) is
only included if the client itself sent a `routing` block – legacy clients see an unchanged body. For `/v1` it is in the
`chat.completion` or in the last SSE chunk.

Size limits before the backend (413): `max_images` 16, `max_tools` 128, `max_messages` 1000, body 64 MiB.

## Scheduler, circuit breaker, admission

- **Score** among the candidates of a tier: warm +100, per running request −10, full node −50, free VRAM (share) ×2, weight ×1,
  speed (EWMA tok/s / 100) ×2, error rate of the last 20 results ×20, breaker probe −5. Weights in `scheduler.score`, editable in
  the UI.
- **Statistics per `model@node`:** mean of the last 20 warm runs and EWMA (α 0.3) for tok/s and time to first token, results
  ok / error / timeout / structured_error. Persisted in `perf.json`.
- **Circuit breaker per node:** three backend errors in 60 s → `open` (30 s), then `half_open` with exactly one probe. A restart of
  the node resets it. Retry on another node on connection error or 5xx before the first byte.
- **Model switch:** while a node is loading a model, further requests that would need that node wait for the load to
  finish instead of getting 503 (event `wait_load`).
- **Admission:** at most `max_inflight` concurrent requests per node (policy in the registry, default 2 – should match the node's
  `OLLAMA_NUM_PARALLEL`). Further requests wait in the router; when a slot frees up, the best rank from priority class and age
  wins (`aging_s` 30 s so that batch does not starve). `max_queue` exceeded → 503 immediately.

## Auto role: the decision engine

A client that does not know which role fits asks `auto:latest`. A chain of engines picks one of the configured roles; uncertain
(small gap between rank 1 and 2, high entropy, low probability) means: next engine, at the end `default`.
Design: [design/decision-engine.md](design/decision-engine.md) (German), measurements: [decision-eval/README.md](decision-eval/README.md) (German).

| Engine | What | Top-1 on 132 unseen test cases | Latency | Where |
|---|---|---|---|---|
| `embed` | `multilingual-e5-small` as ONNX int8 (113 MB) + softmax head, own container ([decision-embed/](decision-embed/)) | **0.932** | 10–19 ms | CPU, 4 cores, 456 MiB |
| `local_llm` | small model through the router itself with an enforced JSON schema | **0.977** | ~200 ms warm | GPU node |
| `tfidf` | character n-grams + softmax regression, 570 KB JSON in-process ([decision-eval/train_tfidf.py](decision-eval/train_tfidf.py)) | 0.848 | < 1 ms | in the router |
| `rules` | keywords, deterministic, end of chain | 0.523 | 0.1 ms | in the router |
| `jevlike` | [Jevlike](https://github.com/vinnylarouge/jevlike) adapter ([decision-jevlike/](decision-jevlike/)); built, measured, not worth it for fixed roles | 0.750 | 15 ms | own service |

Recommended chain `[embed, local_llm, tfidf, rules]`: embed decides in under 20 ms, when uncertain (~6 % of cases) the router asks
the LLM, and if the GPU node is down, tfidf and rules take over. Routing accuracy of the chain 0.947; CPU only
(`[embed, tfidf, rules]`) 0.909.

```yaml
decision_engine:
  enabled: true
  role: auto                                   # appears as auto:latest in /api/tags
  options: [standard, gross, assist, code]
  default: standard
  chain: [embed, local_llm, tfidf, rules]
  policy: { min_top_probability: 0.5, min_margin: 0.2, max_entropy_ratio: 0.75 }
  tfidf: { model_path: /etc/ollama-router/decision-tfidf.json }
  embed: { endpoint: http://127.0.0.1:8082, timeout_s: 2 }
  local_llm: { model: "assist:latest", timeout_s: 20, descriptions: { code: "programming, scripts, debugging", ... } }
  capture: { enabled: false, path: /var/lib/ollama-router/decisions.jsonl, clients: [], anonymize: true }
```

The result is in `routing.decision` (distribution, engine, latency, uncertainty reasons, fallback trace), in the metrics
`skirnir_decision_*` and in the decision log. `POST /admin/decide` queries one engine directly, `GET /admin/decision` shows chain,
policy and health. **Capture** writes training data (JSONL) only for opted-in clients, anonymised (keys, e-mail, IP, URL, long
numbers) and with a group hash; the clients' real role choices (`client`) stay separate from engine pseudo-labels (`engine`).
The embedding head retrains from that in seconds; the embedding model stays.

## Client identities on the inference port

`router.client_auth` knows the clients and introduces them in two phases: `mode: observe` serves everything, counts unknowns and
writes them to the audit log; `mode: enforce` answers without a valid identity with 401 (Ollama or OpenAI error format).
`/` and `/api/version` stay open. `locked: true` prevents the mode from being changed via UI or API.

Three ways, in this order: `Authorization: Bearer <token>` (this is how the Home Assistant Ollama integration sends its API key,
OpenAI clients likewise), `Authorization: Basic <client>:<token>` and **source IP** for clients that cannot send a header
(e.g. `node-red-contrib-ollama`, which only sends its key to ollama.com). A wrong token does not fall back to the IP, it counts as
`bad_token`. Tokens are 256 bits of randomness, only their **sha256** is stored – the check runs on every request, PBKDF2 would be
self-sabotage here.

```yaml
client_auth:
  mode: enforce
  locked: true
  audit_log: /var/log/ollama-router/audit.jsonl
  clients:
    home-assistant: { token_sha256: "…", roles: ["*"], models: true, requests_per_minute: 120 }
    node-red:       { token_sha256: "…", ip: ["192.0.2.20"], roles: ["*"], models: true }
    batch-jobs:     { token_sha256: "…", roles: ["gross"], models: false, max_priority: batch, cloud: false, data_class: internal }
```

Per client: `roles`, `models` (concrete model names allowed?), `requests_per_minute` (429), `max_priority`, `cloud`, `data_class`.
Violations return 403 and go to the audit log (`auth_denied`, `bad_token`, `forbidden`, `rate_limited` – never prompts, never
tokens). Clients can also be created in the UI; the router generates the secret, shows the plain text exactly once and stores the
hash in `roles.yaml`. The **Try** button uses the internal identity `skirnir-ui`, which is only valid from localhost.

## Cloud as a tier in roles

Cloud providers are **models in the catalogue** (`"openai:gpt-5-mini": {cloud: openai, provider_model: gpt-5-mini, price_chf_per_m: {...}}`)
and sit as a tier in exactly those roles that are allowed to use them. Concrete cloud model names are not directly callable
(404), only via roles. A cold local model wins over the cloud. Three cases in which the cloud gets its turn: no local node can
(all gaming or off), a capability is missing locally, or the client demands it (`routing.execution: cloud`).

Limits (`router.cloud`):

- **Data classes as a declaration**, not a content classifier: `routing.data_class`, else `clients.<c>.data_class`, else
  `default_data_class` (**personal**). Cloud only up to `max_cloud_data_class` (**internal**). Consequence: whoever declares nothing
  never goes to the cloud.
- **Credential scan** (`block`): prompts with recognisable keys (`sk-…`, `AKIA…`, `ghp_…`, JWT, `PRIVATE KEY`, `password: …`)
  do not go to the cloud; locally they run normally.
- **Budget** per provider and month in CHF; exhausted = the provider drops out as a tier, from `warn_at_percent` on a Home
  Assistant problem is raised. Cost per request in usage and metrics.
- **Egress allowlist** for `base_url` (+ `egress_allow`), keys only from `secrets.env`, never in the log; circuit breaker as
  with nodes; shadow runs never go to the cloud.

Adapters: `openai` (also for OpenAI-compatible endpoints) and `anthropic` (Messages API). Both translate into the Ollama format,
after which the same paths as for nodes apply.

## Observability

`GET /metrics` returns Prometheus text without `prometheus_client`, prefix `skirnir_`: `requests_total{role,node,model,client,via,outcome}`,
`tokens_total{kind,…}`, histograms `request_duration_seconds`, `ttft_seconds`, `queue_wait_seconds`, `events_total{event,node}`,
per node `node_up`, `node_state`, `node_inflight`, `node_gpu_util_percent`, `node_vram_*_gib`, `node_breaker`, per model
`model_loaded_gib`, `perf_gen_tps`, `perf_error_rate`, plus `decision_*`, `cloud_*`, `usage_today_*`, `info`.

```yaml
scrape_configs:
  - job_name: skirnir
    scheme: https
    metrics_path: /metrics
    basic_auth: { username: metrics, password_file: /etc/prometheus/skirnir.pass }
    static_configs: [{ targets: ["router.example.net:11435"] }]
```

`usage.json` keeps requests, tokens, errors and cloud costs per day and client for 90 days (`GET /admin/usage`). The counters
and histograms behind `/metrics` persist in `metrics.json` next to the configuration (written a minute after a change and at
shutdown), so a deploy no longer resets them; the UI labels them 'total' since the recording began. For history over days
and weeks point a Prometheus at `/metrics` (scrape config above); the router is not a time-series database. [monitoring/](monitoring/)
ships exactly that: Prometheus with 90 days of retention and a Grafana dashboard as two containers on the router host. The decision
log (`/admin/state`, last 2000 entries, persisted in `events.jsonl` next to the configuration so it survives restarts; `?decisions=N` selects how many are returned, default 50) holds the most recent routes and events (busy/free, WOL, breaker, queue, shadow, decision). `/admin/state`
also shows `build` (deploy manifest) and `supply_chain` (Ollama version and model digests per node).

## Web UI

![Overview page of the web UI with live charts and node table](design/ui-uebersicht.png)

*Overview in the local development environment (`test/dev_env.py`) with two fake nodes and synthetic traffic. The UI is available in English and German (toggle in the header).*

One page ([router/ui.html](router/ui.html)), no external libraries, six tabs:

- **Overview:** nodes with state, breaker, GPU, VRAM, load and loaded models; cloud providers with budget bars; operations tiles; recent decisions; **live charts** (canvas from `/metrics`): requests/min,
  tokens/s, GPU and VRAM per node, daily history, latency histogram, shares by node/role/client, effectively used models.
- **Agents:** registered agents with facts, approval, per-node policy (WOL, weight, busy thresholds, GPU guard, MQTT,
  auto-update), the staged agent version and the update state; a dot on the tab while an agent waits for approval.
- **Roles:** tier editor with priority, canary, shadow; **Try** (role or model, num_ctx, execution, data class, priority, think)
  with node, tier, reason, skipped tiers, duration split into queue / model / router; **load test** (n requests with concurrency c,
  plus probes with `interactive`).
- **Clients:** one card per client, generate and rotate secrets, source IPs, roles, limits.
- **Model catalogue:** weights, context cost, VRAM need, speed, capabilities, measure and benchmark; cloud models with prices.
- **Settings:** everything the router reads at runtime (allowlist in [settings.py](router/ollama_router/settings.py)), with the
  config.yaml value, changed values marked, and reset.

Look at it locally without real nodes: `python test/dev_env.py` starts fake nodes, fake cloud, fake agent and the router without
TLS and login at `http://127.0.0.1:21435/`.

## Home Assistant

The router registers via MQTT discovery as the device **Skirnir**: sensors for nodes online/busy, available models, ready roles
(attribute: which node and model would be chosen right now), requests and tokens today, cloud costs this month, last assignment,
per node state / GPU utilisation / free VRAM, a `binary_sensor` **Problem** with attribute `problems` (no node online, role not
servable, agent silent, budget warning) and a sensor **Node awaiting approval** for new agents. Last will
`ollama-router/status=offline`; missing readings are `unavailable`, not `unknown`. Node entities are kept in sync: blocked or
deleted nodes disappear from Home Assistant, even if they vanished during a router restart. The Home Assistant Ollama integration
talks to the router directly (`https://router.example.net:11434`, API key = client token); model = role.

## GPU nodes: the agent

[agent-go/](agent-go/) contains the agent as a Windows service or Linux binary (Go, own README in German). It

- builds the tunnel to the router (`wss://…:11435/v1/tunnel`) and keeps it up with backoff; a router restart costs 1–2 s,
- identifies itself with an **Ed25519 key** generated on first start (Windows: DPAPI-protected); unknown keys wait in the router
  for approval (UI, Home Assistant sensor),
- reports GPU utilisation and VRAM every 2 s (NVML directly, fallback `nvidia-smi`), hostname, MAC, versions; since
  agent 0.6.0 also temperature, power draw and limit, fan, throttle reasons, and, if GPU-Z is running on a Windows node,
  memory temperature, hot spot, GPU voltage and 16-pin connector power and voltage (all forwarded to the router state and
  as Home Assistant sensors),
- receives its **configuration package** through the tunnel after approval (heartbeat interval, optional MQTT access) and needs no
  secrets of its own,
- can run **Ollama as a child process** (`children:` in its configuration) so that a node is ready after a reboot without anyone
  logging in, and can optionally supervise further services,
- optionally provides a TLS proxy in front of Ollama (`ollama_proxy`, port 11443, certificate pinned by fingerprint) for routers
  that should access it directly without the tunnel,
- **guards the GPU** (agent 0.7.0, [design/gpu-guard.md](design/gpu-guard.md), German): sets a power limit of 80 % of the
  card's default (RTX 5090: 460 W), drops to 70 % after 10 minutes of sustained full load, re-applies after driver resets,
  never raises above the default, and warns on 16-pin voltage sag, memory temperature and hardware throttling. On by
  default; opting out (`gpu_guard.enabled: false`) is deliberate and therefore reported as a problem. The router caps a
  throttled node to one request, penalises it in the score and forwards problems to Home Assistant (`modes.gpu_guard`,
  per-node policy in the agent registry).

**Updating agents:** `deploy.py --agent` puts the binaries with a manifest signed by an operator key (never by the router)
on the router; agents pull them through their outbound connection, verify signature and hash, swap themselves and restart.
A button per node in the web UI, or the rollout loop with a per-node policy and a canary node that gets new versions first
(`modes.agent_update`). Failures and stalls become Home Assistant problems.

A new GPU machine needs: Ollama with models, the agent binary, a configuration with `router.url` (template
[agent-go/config.example.yaml](agent-go/config.example.yaml)), `Install-Service.ps1` as administrator, then approval in the router
UI (Wake-on-LAN, weight, MQTT device). No token, no firewall rule, no certificate, no IP by hand.

## Installing the router

Requirements: Debian 12 (or comparable) with Python 3.11, packages `python3-aiohttp`, `python3-yaml`, `python3-cryptography`;
`python3-paho-mqtt` for Home Assistant, `python3-uvloop` optional (on Windows the router runs with the default event loop).
A TLS certificate for the router's hostname (e.g. Let's Encrypt) that the clients connect to.

1. Copy `router/` to `/opt/ollama-router/` (including the package `ollama_router/`).
2. Copy [router/config.example.yaml](router/config.example.yaml) to `/etc/ollama-router/config.yaml` (0600) and adapt:
   `public_url`, certificate paths, roles, catalogue, clients. Password hashes for the UI are generated with
   `python3 router.py --hash '<password>'`; client hashes are the `sha256` of the token.
3. Check: `python3 router.py --check /etc/ollama-router/config.yaml` – reports unknown keys with a suggestion.
4. systemd units from `router/` to `/etc/systemd/system/`: `ollama-router.service` (runs with `ProtectSystem=strict`, writes only
   `/etc/ollama-router` and its log directory), optionally `ollama-router-cert.path` (restart on renewed certificate) and
   `ollama-router-secrets.*` (see 5). `systemctl enable --now ollama-router`.
5. Secrets: the router reads `/etc/ollama-router/secrets.env` (`MQTT_PASSWORD`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). The file
   can be maintained by hand or rendered by `render-env.sh` from a secret store (universal-auth API) (credentials in
   `/etc/ollama-router/render-env.conf`, template [router/render-env.conf.example](router/render-env.conf.example); a daily timer
   restarts the router when a secret changed).
6. For the auto role: `decision-tfidf.json` to `/etc/ollama-router/` and start the embedding container from `decision-embed/`
   (`compose.yaml`, binds only `127.0.0.1:8082`). Both are optional; without them `decision_engine.enabled` stays `false`.
7. Install agents and approve them in the UI.

**Deploying from a workstation** (LXC): [deploy/deploy.py](deploy/deploy.py) copies code, configuration and units via SFTP to
the LXC host, from there via file push into the container (templates `CT_EXEC`/`CT_PUSH`, default LXD), checks the
configuration before and after the push and restarts the service only if the check passes. Everything site-specific (host,
container number, production `config.yaml`, secret store access) is read from an **ops folder outside the repo**, see
[deploy/ops_env.py](deploy/ops_env.py). That way the repo can be public without hashes, IPs or hostnames ending up in it. If you
have no LXC host, copy by hand or adapt the two templates `CT_EXEC`/`CT_PUSH` in `deploy.env`.

## Development and tests

```bash
python test/run_tests.py          # about 250 end-to-end checks, ~3 min: two fake Ollama nodes, fake agent through the tunnel,
                                  # fake cloud (OpenAI and Anthropic), fake decision service, self-signed TLS, client auth,
                                  # admission, breaker, canary/shadow, idempotency, cloud limits, UI config round trips
python test/selftest_kontextpruefung.py   # standalone self-tests without a router: context pre-check and
python test/selftest_toolcall_rescue.py   # tool-call rescue (a few seconds each)
python test/dev_env.py            # the same environment for clicking around, without TLS/login
python test/perf_run.py           # test bench: CPU per request, py-spy profile (test/perf_profile_report.py)
ruff check .                      # lint (ruff.toml: F, E9, B904, B905)
```

Dependencies for running and developing: `router/requirements.txt` (runtime, matches the Debian 12 packages) and
`requirements-dev.txt` (lint, deploy, tests). CI runs lint, `go vet`/`go test`, a Windows cross-build and the self-tests on
every push; the end-to-end suite runs nightly because it is timing-sensitive.

Measured, the router is I/O-bound: around 0.6 ms CPU per forwarded request on the test bench, about 10 ms per `/api/chat` in
production versus 200–300 ms in the model. One process with one event loop is enough because the GPU nodes deliver 2–4
requests/s and the actual limit is `OLLAMA_NUM_PARALLEL`.

## Repo layout

| Path | Content |
|---|---|
| `router/router.py`, `router/ollama_router/` | Service: `app`, `config` (schema), `proxy` (Ollama API), `openai_api`, `request` (routing block), `scheduler`, `admission`, `nodes`, `poll` (state machine, prewarm), `registry` (agent registry), `tunnel`, `auth`, `cloud`, `decision/` (engines), `metrics`, `perf`, `ops` (idempotency, canary, shadow, manifest), `ha` (MQTT), `admin`, `settings` (UI allowlist), `toolcall_rescue` (tool calls that Ollama's parser loses as text are recovered), `wol` |
| `router/ui.html`, `router/skirnir.png`, `router/favicon.png` | Web UI and logo |
| `router/config.example.yaml`, `router/*.service`, `*.timer`, `*.path`, `render-env.sh`, `render-env.conf.example` | Example configuration, systemd units, secrets renderer |
| `router/decision-tfidf.json` | trained TF-IDF model of the auto role |
| `agent-go/` | Agent for the GPU nodes (Go 1.27, Windows service / Linux binary), own README |
| `decision-eval/` | Dataset generator (136 templates, 1282 examples, group split), trainers for TF-IDF and Jevlike, evaluation (top-1, calibration, chains), results |
| `decision-embed/` | Stage 2 of the auto role: ONNX export, head training, service, container |
| `decision-jevlike/` | Jevlike service (prototype, measured, not in production) |
| `deploy/` | `deploy.py`, `ops_env.py` |
| `design/` | Design notes: `routing-algorithm.md`, `roadmap.md` (stages and decisions), `decision-engine.md`; logos (generated with an image model, metadata removed) |
| `test/` | Test suite, fakes, dev environment, test bench |
| `tools/` | measurement scripts for the GPU guard (load test, stage-2 night run) |
| `monitoring/` | Prometheus + Grafana next to the router (compose, scrape config, provisioned dashboard, secret renderer, `deploy_ct.py`), own README |
| `.github/workflows/ci.yml`, `CHANGELOG.md`, `SECURITY.md` | CI (lint, Go vet/test, Windows cross-build, self-tests; the end-to-end suite nightly), release history, vulnerability reporting |

### Two names, on purpose

**Skirnir** is the product name: the repository, the Home Assistant device, the `skirnir_*` metrics, the `X-Skirnir-*`
headers, the container images. **`ollama-router`** is the frozen technical name underneath: the Python package
`ollama_router`, the systemd units and paths (`/opt/ollama-router`, `/etc/ollama-router`), the MQTT base topic and discovery
ids, the Windows service `OllamaRouterAgent` with its virtual account, scheduled task and `ProgramData` folder, and the
agent binary and release asset names. Renaming those would break every installed node and every Home Assistant entity
history, and a service rename cannot travel through the self-update. So the technical names stay as they are; new
features use the Skirnir name where a user sees it.

## Security model, in short

- All links TLS: inference and admin port with the router's own certificate, tunnel over WSS with an Ed25519 challenge per agent,
  MQTT over 8883. Ollama itself listens on localhost only.
- Two trust levels: the inference port knows clients (token hash or IP), the admin port requires Basic Auth with PBKDF2.
- Whatever shifts the trust base (ports, TLS, identities, provider endpoints, egress, `client_auth.mode` when `locked`) cannot be
  changed via UI or API, only via the configuration file.
- Secrets live in `secrets.env` (0600), never appear in logs or `/admin/state`; the router has no write access to the secret store.
- Prompts go to the cloud only with a declared data class and never with recognisable keys in them.
- Open: the service runs as root (a dedicated user needs rights on configuration, log and certificate).

## Measurements as a reference

VRAM need according to the formula above, measured on an RTX 5090 (31.8 GiB, KV cache f16, Ollama 0.33/0.34); speed from the
UI benchmark (200 tokens, temperature 0, median of two runs, context 8k).

| Model | Weights GiB | Context cost MiB/1k | VRAM @8k / @32k / @64k GiB | gen tok/s | prompt tok/s | Capabilities |
|---|---|---|---|---|---|---|
| qwen3.6:35b-a3b | 20.6 | 1 | 21.4 / 21.4 / 21.5 | 254 | 1219 | vision, tools, thinking |
| qwen3-coder:30b | 17.3 | 98 | 18.9 / 21.2 / 24.2 | 285 | 5964 | tools |
| glm-4.7-flash | 17.7 | 50 | 18.9 / 20.1 / 21.7 | 224 | 10689 | tools, thinking |
| granite4.2:30b | 16.8 | 248 | 19.5 / 25.3 / 33.1 ✗ | 76 | 3478 | tools, thinking |
| gemma4:26b | 16.1 | 12 | 17.0 / 17.3 / 17.7 | 233 | 1822 | vision, tools, thinking |
| gpt-oss:20b | 12.0 | 2 | 12.8 / 12.8 / 12.9 | 270 | 6712 | tools, thinking (no `structured`) |
| gemma4:12b (RTX 4080) | 7.8 | 2 | 8.6 / 8.6 / 8.7 | 71 | 1277 | vision, tools, thinking |
| granite4.2:8b | 5.0 | 162 | 7.0 / 10.8 / 15.9 | 213 | 7919 | tools, thinking |

Models with hybrid attention (qwen3.6, gemma4, gpt-oss) cost practically no VRAM per context token; granite and the coder models
pay noticeably. Thinking models produce invisible reasoning tokens unless `think: false` is set – the response time is then no
measure of speed. Re-measure when Ollama, driver or KV cache type change.

## Limits and non-goals

- One router process, not a cluster: shared state (queue, breaker, registry) lives in memory and in JSON files.
- No content classifier for data classes, no model signing beyond Ollama's digests, no OpenTelemetry.
- Busy detection on Windows has no per-process VRAM (WDDM reports `N/A`); it works with totals and learned baselines and has
  hold-off and claim mechanics against phantom values in return.
- `/v1/embeddings` needs an embedding model on the node; chat models answer with 501 there, which the router passes through.
- The deploy scripts assume LXC.

## License

[MIT](LICENSE). The models Skirnir distributes and the third-party projects involved (Jevlike, multilingual-e5-small, Ollama)
have their own licenses.
