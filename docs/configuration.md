# Configuration

**English** · [Deutsch](configuration.de.md) · [README](../README.md)

## Files

| File | Purpose |
|---|---|
| `/etc/skirnir-router/config.yaml` | The configuration (0600). Start from [router/config.minimal.yaml](../router/config.minimal.yaml) (a small, safe start: one role, one client; `router.py --init` fills it in, see [install-router.md](install-router.md#first-configuration)) or [router/config.example.yaml](../router/config.example.yaml) (the full reference with every option and its comment). |
| `/etc/skirnir-router/roles.yaml` | Overrides written by the web UI next to `config.yaml`; survives deploys and upgrades. |
| `/etc/skirnir-router/secrets.env` | Secrets as environment ([install-router.md](install-router.md#secrets)). |

Roles, catalogue and most runtime settings are editable in the UI; the UI writes them to `roles.yaml`. Ports, TLS, Basic Auth,
identities, provider endpoints and the egress allowlist remain deploy matters on purpose. Catalogue entries from both files
are merged field by field: a measurement in `roles.yaml` replaces the size fields (weights, KV, measured values) as a unit,
while other fields from `config.yaml` such as `max_parallel` or capability overrides keep applying.

After every change: `python3 router.py --check /etc/skirnir-router/config.yaml` (unknown keys are reported with a suggestion;
`--pure` ignores `roles.yaml`) and `python3 router.py --posture /etc/skirnir-router/config.yaml`.

## Client identities on the inference port

`router.client_auth` knows the clients and introduces them in two phases: `mode: observe` serves everything, counts unknowns and
writes them to the audit log; `mode: enforce` answers without a valid identity with 401 (Ollama, OpenAI or Anthropic error format).
`/` and `/api/version` stay open. `locked: true` prevents the mode from being changed via UI or API.

Three ways, in this order: `Authorization: Bearer <token>` (this is how the Home Assistant Ollama integration sends its API key,
OpenAI clients likewise; Anthropic clients may send the token as `x-api-key` instead), `Authorization: Basic <client>:<token>`
and **source IP** for clients that cannot send a header (e.g. `node-red-contrib-ollama`, which only sends its key to
ollama.com). A wrong token does not fall back to the IP, it counts as `bad_token`. Tokens are 256 bits of randomness, only
their **sha256** is stored – the check runs on every request, PBKDF2 would be self-sabotage here.

```yaml
client_auth:
  mode: enforce
  locked: true
  audit_log: /var/log/skirnir-router/audit.jsonl
  clients:
    home-assistant: { token_sha256: "…", roles: ["*"], models: true, requests_per_minute: 120 }
    node-red:       { token_sha256: "…", ip: ["192.0.2.20"], roles: ["*"], models: true }
    batch-jobs:     { token_sha256: "…", roles: ["gross"], models: false, max_priority: batch, cloud: false, data_class: internal }
```

| Per client | Meaning |
|---|---|
| `roles` | Roles the client may use (`["*"]` or missing = all) |
| `models` | Concrete model names allowed? ([ADR-0052](adr/0052-concrete-model-names-need-an-explicit-grant.md)) |
| `requests_per_minute` | Rate limit; above it 429 |
| `max_concurrent` | Requests in progress at once, default 4; the next one gets 429 with `Retry-After: 1` ([ADR-0063](adr/0063-bound-concurrency-and-body-size-per-client.md)) |
| `max_body_mib` | Declared body size, default 16; above it 413 (ADR-0063) |
| `max_priority` | Highest admission priority the client may claim |
| `cloud`, `data_class` | Cloud tiers allowed, declared data class ([below](#cloud-as-a-tier-in-roles)) |
| `privacy_check` | Content check before a cloud tier (below) |
| `rerank` | Right to use the [rerank endpoint](reference.md#optional-rerank-endpoint) |
| `inference` | `false` = only the rerank endpoint, everything else 403 |
| `ip` | Source addresses for clients without a header |

Refusals go to the audit log, never with prompts or tokens: `auth_denied` (no valid identity under `enforce`, 401; under
`observe` it is logged as `auth_missing`), `bad_token`, `forbidden` (role, concrete model or inference not allowed, 403),
`rate_limited` and `concurrency_limited` (429), `body_too_large` (413). Clients can also be created in the UI; the router
generates the secret, shows the plain text exactly once and stores the hash in `roles.yaml`. The **Try** button uses the
internal identity `skirnir-ui`, which is only valid from localhost.

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
  Reference values: [operations.md](operations.md#measurements-as-a-reference).
- **Busy.** A node is `busy` when foreign VRAM (used − Ollama − desktop baseline) exceeds the threshold or the GPU is loaded
  without router requests. Then only `busy_ok` tiers run; non-`busy_ok` models are unloaded every 30 s. The desktop baseline is
  learned per node (median of hourly means while the GPU is quiet) and capped by a policy value. Leaving `busy` is easier
  than entering it (hysteresis): the node stays busy while the load is above `busy_exit.gpu_util_pct` (20 %) and foreign VRAM
  is in use, and only after `below_for_s` of quiet does it become free - a light game hovering around the entry threshold
  otherwise flipped the node every few seconds.
- **Game and memory as facts (agent 0.17.0, Windows).** The agent reports whether a game is running (process path under a
  Steam library or the install folder of a known game publisher; by the name of a large program in such a folder when
  the path is not readable) and the free main memory (available RAM and free commit). A running game makes the node `busy`
  after `busy_enter.game_sustain_s` (10 s), even when it uses little VRAM, and keeps it busy for `busy_exit.game_quiet_s`
  (60 s) after it ends. Since router 0.8.7 / agent 0.19.0 this applies only to a game that needs the GPU: the agent also
  reports the game's own GPU use (dedicated + shared graphics memory, highest engine load, over all game processes), and
  the game counts once it reaches `busy_enter.game_min_gpu_gib` (2 GiB) or `game_min_gpu_pct` (10 %), or during memory
  pressure; from then on it holds the node busy until it ends. Shared memory counts because a game that gets no VRAM next
  to a large model moves there. A light game (measured: 735 MiB, 2 %) leaves the node free, and prewarm still runs; a
  prewarm skipped while the game was still being judged runs once it counts as light or has ended (0.8.8). An
  agent that does not report GPU use gets the old behaviour. `game_heavy` and its reason are in `/admin/state`.
  When available RAM or free commit stays below `memory_pressure.min_free_gib` (8 GiB) for
  `memory_pressure.polls` heartbeats (2), the node is under memory pressure: nothing is loaded cold and no prewarm runs,
  loaded models keep answering and nothing is unloaded. Reason code `memory_pressure` with `Retry-After` 60 s.
  `skirnir-agent game` prints what the agent detects.
- **Prewarm.** After coming online and after `busy → free` the router preloads the rank-1 models of the roles, as many as fit
  into VRAM together. A **residency rule** brings back an evicted rank-1 model once the evictor has not been requested for
  5 min – otherwise "warm first" would stay stuck on the fallback tier forever.
- **Concrete models** (`qwen3-coder:30b`) remain directly callable (`expose_concrete_models`) and go 1:1 to a node that has
  them; otherwise 404 as with Ollama. A client needs `models: true` for them; without the setting it gets roles only and
  the router warns at load time (ADR-0052).
- **Canary** (`roles.<r>.canary: {model, num_ctx, percent}`) sends a share of the traffic to a candidate, even cold.
  **Shadow** (`roles.<r>.shadow`) repeats a share of the requests after the response, without streaming, against a second model on
  a free node; the client notices nothing, speed and error rate end up in the statistics.

## Scheduler, circuit breaker, admission

- **Score** among the candidates of a tier: warm +100, per running request −10, full node −50, free VRAM (share) ×2, weight ×1,
  speed (EWMA tok/s / 100) ×2, error rate of the last 20 results ×20, breaker probe −5, switching to a standby Ollama −60
  ([reference.md](reference.md#optional-openai-compatible-node)). Weights in `modes.score` (keys `warm`, `inflight`,
  `saturated`, `vram_free`, `weight`, `speed`, `errors`, `half_open`, `switch`), editable in the UI; enter only weights that
  differ from the defaults.
- **Statistics per `model@node`:** mean of the last 20 warm runs and EWMA (α 0.3) for tok/s and time to first token, results
  ok / error / timeout / structured_error. Persisted in `perf.json`.
- **Circuit breaker per node** (`modes.breaker`): three backend errors in 60 s → `open` (30 s), then `half_open` with exactly
  one probe. A restart of the node resets it. Retry on another node on connection error or 5xx before the first byte.
- **Model switch:** while a node is loading a model, further requests that would need that node wait for the load to
  finish instead of getting 503 (event `wait_load`).
- **Admission** (`modes.admission`): at most `max_inflight` concurrent requests per node (policy in the registry, default 2 –
  should match the node's `OLLAMA_NUM_PARALLEL`). Further requests wait in the router; when a slot frees up, the best rank from
  priority class and age wins (`aging_s` 30 s so that batch does not starve). `max_queue` exceeded → 503 immediately.
- **Models without parallel requests:** `models.<m>.max_parallel` caps concurrent requests per node for one model. qwen3.8
  (architecture qwen35) cannot run two requests at once in Ollama; a second request sent anyway waits in Ollama's own queue,
  and Ollama then reloads the runner as soon as the first one finishes (5–7 s, context checkpoints and prompt cache lost).
  With `max_parallel: 1` the second request waits in the router instead, with priority, and the runner stays.
- **Waiting budget per priority class** when the request sets no `deadline_ms`: `max_wait_interactive_s` (60 s; a voice
  command that waited a minute is lost anyway), `max_wait_s` for normal (600 s; agent requests often take minutes) and
  `max_wait_batch_s` (1800 s).
- **Interactive requests do not queue behind a busy warm model** if another local tier can serve right away: first a free
  warm tier, then a free cold one, other nodes before the busy one (never a cloud tier on this path); the decision log shows
  `reason: fallback`. Without a busy warm model, warm first decides as before; if no tier is free, the request waits
  within its interactive budget.
- **Observation mode** (`decision_engine.observe_blocked`): when a request for a role has to wait, the decision engine
  classifies it in the background with its CPU chain only (embedding, TF-IDF, rules; never the LLM, which would queue on
  the very GPU being waited for), and the decision log records `decision_observe`: class, confidence, the first free lower
  tier of the same role and `would_fallback` for the classes in `observe_light`. Routing does not change; the point is to
  measure how often a free smaller model would have been good enough before letting the router act on it.
  `observe_all` goes further: every request to `/api/chat` or `/api/generate` is classified in the background when it starts,
  and when it ends the router appends one line to `requests.jsonl` next to the configuration (`observe_path`): class,
  duration, waiting time, tokens, thinking, tool count, client, role, outcome - no prompt contents. A separate file, so the
  decision log keeps its window; it rotates at 20 MB. The response never waits for the classification.

## Auto role: the decision engine

A client that does not know which role fits asks `auto:latest`. A chain of engines picks one of the configured roles; uncertain
(small gap between rank 1 and 2, high entropy, low probability) means: next engine, at the end `default`.
Design: [design/decision-engine.md](../design/decision-engine.md) (German), measurements:
[decision-eval/README.md](../decision-eval/README.md) (German).

| Engine | What | Top-1 on 132 unseen test cases | Latency | Where |
|---|---|---|---|---|
| `embed` | `multilingual-e5-small` as ONNX int8 (113 MB) + softmax head, own container ([decision-embed/](../decision-embed/)) | **0.932** | 10–19 ms | CPU, 4 cores, 456 MiB |
| `local_llm` | small model through the router itself with an enforced JSON schema | **0.977** | ~200 ms warm | GPU node |
| `tfidf` | character n-grams + softmax regression, 570 KB JSON in-process ([decision-eval/train_tfidf.py](../decision-eval/train_tfidf.py)) | 0.848 | < 1 ms | in the router |
| `rules` | keywords, deterministic, end of chain | 0.523 | 0.1 ms | in the router |
| `jevlike` | [Jevlike](https://github.com/vinnylarouge/jevlike) adapter ([decision-jevlike/](../decision-jevlike/)); built, measured, not worth it for fixed roles | 0.750 | 15 ms | own service |

The table is one split of the earlier data set (2026-09-17). Retrained on the neutral data (router 0.25.1, ADR-0077)
and measured over 50 splits of the templates: tfidf 0.91 ± 0.06, embed 0.96 ± 0.03, the same as before within the
spread; one split alone moves top-1 by several points
([design/decision-training-data.md](../design/decision-training-data.md)).

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
  tfidf: { model_path: /etc/skirnir-router/decision-tfidf.json }
  embed: { endpoint: http://127.0.0.1:8082, timeout_s: 2 }
  local_llm: { model: "assist:latest", timeout_s: 20, descriptions: { code: "programming, scripts, debugging", ... } }
  capture: { enabled: false, path: /var/lib/skirnir-router/decisions.jsonl, clients: [], anonymize: true }
```

`config.example.yaml` ships this block enabled with the chain `[embed, local_llm, tfidf, rules]`. The tfidf file and the embed
container are optional ([install-router.md](install-router.md#optional-decision-engine-for-the-auto-role)): if the tfidf file
cannot be loaded, the router logs a warning at start and leaves the engine out; if the embed container does not answer, each
decision logs a warning and continues with the next engine. Without them set `enabled: false` or a shorter chain such as
`[local_llm, rules]`. `local_llm` needs `descriptions` for the roles; the names alone tell the model nothing.

The result is in `routing.decision` (distribution, engine, latency, uncertainty reasons, fallback trace), in the metrics
`skirnir_decision_*` and in the decision log. `POST /admin/decide` queries one engine directly, `GET /admin/decision` shows chain,
policy and health. **Capture** writes training data (JSONL) only for opted-in clients, anonymised (keys, e-mail, IP, URL, long
numbers) and with a group hash; the clients' real role choices (`client`) stay separate from engine pseudo-labels (`engine`).
The embedding head retrains from that in seconds; the embedding model stays.

## Cloud as a tier in roles

Cloud providers are **models in the catalogue** (`"openai:gpt-5-mini": {cloud: openai, provider_model: gpt-5-mini, price_chf_per_m: {...}}`)
and sit as a tier in exactly those roles that are allowed to use them. Concrete cloud model names are not directly callable
(404), only via roles. A cold local model wins over the cloud. Three cases in which the cloud gets its turn: no local node can
(all gaming or off), a capability is missing locally, or the client demands it (`routing.execution: cloud`).

Limits (`router.cloud`):

- **Data classes as a declaration**, not a content classifier: `routing.data_class`, else `clients.<c>.data_class`, else
  `default_data_class` (**personal**). Cloud only up to `max_cloud_data_class` (**internal**). Consequence: whoever declares nothing
  never goes to the cloud.
- **Content check instead of a blanket block** (router 0.13.0, opt-in per client: `clients.<c>.privacy_check: true` and
  `router.cloud.privacy_check.enabled: true`): for a client whose class is `personal` by default (not declared), the cloud tier
  stays a candidate, and when it is actually chosen the request is checked: rules on what the user wrote (operator `terms`
  such as family names, e-mail, phone, IBAN, AHV, street address, built-in keyword groups for health, finances, credentials,
  relationships, law), then a classifier on the router host (decision-embed `POST /privacy`, e5-small plus a trained head,
  a few ms). Above its threshold, on a rule hit, or when the classifier does not answer (fail closed) the request stays
  local and gets the configured `refusal` sentence as its answer. Before an allowed request leaves, tool results are redacted:
  Home Assistant live-context entries of `redact_domains` (person, device_tracker, todo, lock, camera, ...),
  `redact_device_classes` (motion, presence, door, window, ...) and `redact_names`, and the whole result of `redact_tools`.
  The journal records `privacy_check` with outcome, rule or probability and the number of redacted entries, never the text.
  A declared `routing.data_class` is never overruled.
- **Credential scan** (`block`): prompts with recognisable keys (`sk-…`, `AKIA…`, `ghp_…`, JWT, `PRIVATE KEY`, `password: …`)
  do not go to the cloud; locally they run normally.
- **Budget** per provider and month, in the currency of the catalogue prices (the router does not convert currencies; the
  suffix `_chf` of keys, fields and metrics and the unit of the Home Assistant cost sensor are historical); exhausted =
  the provider drops out as a tier, from `warn_at_percent` on a Home Assistant problem is raised. Cost per request in usage
  and metrics.
- **Egress allowlist** for `base_url` (+ `egress_allow`), keys only from `secrets.env`, never in the log; circuit breaker as
  with nodes; shadow runs never go to the cloud.
- **Transport:** providers require the final HTTPS endpoint. HTTP is supported only for explicitly allowlisted literal
  loopback addresses (127.0.0.1 and ::1). Provider redirects return an upstream error.

Adapters: `openai` (also for OpenAI-compatible endpoints) and `anthropic` (Messages API). Both translate into the Ollama format,
after which the same paths as for nodes apply.

**Further OpenAI-compatible providers** (for example Mistral's EU endpoint `https://api.eu.mistral.ai/v1`) need no code:
a provider with `kind: openai`, its `base_url`, `api_key_env` and `secrets_file`, the host in `router.cloud.egress_allow`, and
catalogue entries named `<provider>:<model>` with `cloud: <provider>` and prices. Their order inside a role decides which
provider is asked first. With `render-env.sh`, name the key in `render-env.conf` as `EXTRA_SECRET_KEYS="EXAMPLE_API_KEY"` so it
reaches `secrets.env` (ADR-0080); names must end in `_KEY` or `_TOKEN`, because `secrets.env` is the router unit's environment. See the disabled `mistral` provider in [config.example.yaml](../router/config.example.yaml).
