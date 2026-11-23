# Configuration

**English** · [Deutsch](configuration.de.md) · [README](../README.md)

## Files

| File | Purpose |
|---|---|
| `/etc/skirnir-router/config.yaml` | The configuration (root, 0640: the router reads it through its group `skirnir-router`, ADR-0086). Start from [router/config.minimal.yaml](../router/config.minimal.yaml) (a small, safe start: one role, one client; `router.py --init` fills it in, see [install-router.md](install-router.md#first-configuration)) or [router/config.example.yaml](../router/config.example.yaml) (the full reference with every option and its comment). |
| `/var/lib/skirnir-router/roles.yaml` | Overrides written by the web UI into the router's state folder (`router.state_dir`); survives deploys and upgrades. The router writes all its own files there - `roles.yaml`, `nodes.json`, journals, metrics - and nothing in `/etc/skirnir-router` (ADR-0086). Without `state_dir` they lie next to `config.yaml`, which only suits development: the service's account cannot write `/etc/skirnir-router`. |
| `/etc/skirnir-router/secrets.env` | Secrets as environment (root, 0640 for the router's group; [install-router.md](install-router.md#secrets)). |

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
    options: { temperature: 0 }           # sampling defaults for the local tiers (ADR-0093)
    system_append:                        # text only for the model that serves (ADR-0094)
      granite4.2:8b: "Answer in at most two sentences."
    tiers: [...]
```

- **Sampling defaults per role** (`roles.<r>.options`, router 0.30.0, [ADR-0093](adr/0093-sampling-defaults-per-role.md)). Keys:
  `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `frequency_penalty`, `repeat_penalty` (Ollama names). The
  router fills them into a request for the role when the client leaves them out; a value the client sends wins. They go to
  the role's local tiers only: Ollama nodes get them as options, OpenAI-compatible nodes (llama.cpp, Strata) as request
  fields (`repeat_penalty` as `repetition_penalty`); a cloud tier never gets them, since some cloud models refuse a
  temperature other than their default. A request for a concrete model gets none. Unknown keys, wrong types and values out
  of range stop the configuration from loading. Like `priority`, put them in `config.yaml`: a role saved from the web UI
  does not carry them.
- **System text per model** (`roles.<r>.system_append`, router 0.31.0, [ADR-0094](adr/0094-system-text-per-model-of-a-role.md)).
  Maps catalogue models to text (at most 8000 characters each). When a model with an entry serves a chat request for
  the role, the router appends its text to the leading system message (or sends it as a new leading system message);
  every tier with that model gets it, including a canary, no other model does. Use it for prompt rules one model
  needs and another one does not. `/api/generate`, embeddings and requests for a concrete model are unchanged. The
  route entry in the journal records the appended length, not the text. A model missing from the catalogue, non-text
  or an over-long text stops the configuration from loading. Like `options`, put it in `config.yaml`; a role saved
  from the web UI keeps it. The text reaches a cloud tier like the client's system prompt and is not classified by
  the privacy check, so it must not contain personal data.
- **Ranking = quality ceiling and cold-start order.** If a listed model is already loaded on any node, it wins (among several warm
  ones, the highest ranked). Globally `modes.warm_first`, per role `latency_first`.
- **Context ladder.** The same model with a smaller `num_ctx` is the next tier when the context does not fit into VRAM.
  Fit check: `VRAM need = weights + context cost × num_ctx/1000 + 0.8 GiB`. Weights and context cost per model live in the
  catalogue (`models:`), measured with the UI button **Measure** (loads at 8k and 32k, reads `/api/ps`) or estimated.
  Reference values: [operations.md](operations.md#measurements-as-a-reference).
- **Benchmark.** The UI button **Benchmark** measures generation and prompt speed of a loaded model (one warm-up, two runs,
  200 tokens, temperature 0) with `router.benchmark.prompt` (router 0.37.0,
  [ADR-0106](adr/0106-language-dependent-defaults-live-in-the-configuration.md)). The default is the German prompt of all
  earlier measurements, unchanged. Tokens per second depend on the language of prompt and answer (tokenizers split
  languages into differently many tokens), so set a prompt in your clients' language and compare only results with the
  same `prompt_id`, which every result records.

  ```yaml
  router:
    benchmark:
      prompt: "Explain in about 200 words what a network router does and how it differs from a switch."
  ```
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
  Since agent 0.36.0 `game.enabled: false` in the agent configuration switches detection off (ADR-0103); the agent then
  reports `enabled: false` and the router knows no game for the node. A node without a GPU (`gpu_present: false`,
  ADR-0102) has no GPU busy detection; the router sizes its models by the memory facts below.
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
  finish instead of getting 503 (event `wait_load`). The same holds before a cold pick: if a local tier ranked before the
  picked tier is loading its own model, the request waits once for that load (at most until its admission deadline)
  instead of going to the cloud (ADR-0095) or loading a lower model beside it (ADR-0096). A lower tier that is already
  loaded is still taken at once.
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
  `reason: fallback`. A cold tier on the busy node is taken only if it fits beside the model that is answering there;
  one that would need that model's memory would evict it as soon as its request ends (ADR-0097). Without a busy warm
  model, warm first decides as before; if no tier is free, the request waits within its interactive budget.
- **Observation mode** (`decision_engine.observe_blocked`): when a request for a role has to wait, the decision engine
  classifies it in the background with its CPU chain only (embedding, TF-IDF, rules; never the LLM, which would queue on
  the very GPU being waited for), and the decision log records `decision_observe`: class, confidence, the first free lower
  tier of the same role and `would_fallback` for the classes in `observe_light`. Routing does not change; the point is to
  measure how often a free smaller model would have been good enough before letting the router act on it.
  Without `observe_light` the configuration check sets `[standard, assist]`, limited to the names among `options` (router
  0.37.0, [ADR-0106](adr/0106-language-dependent-defaults-live-in-the-configuration.md)); with roles named differently,
  list your light classes. A given list is taken as is, and `[]` means that no class counts as light.
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

Cloud providers are **models in the catalogue** (`"openai:gpt-5-mini": {cloud: openai, provider_model: gpt-5-mini, price_per_m: {...}}`)
and sit as a tier in exactly those roles that are allowed to use them. Concrete cloud model names are not directly callable
(404), only via roles. A cold local model wins over the cloud. Three cases in which the cloud gets its turn: no local node can
(all gaming or off), a capability is missing locally, or the client demands it (`routing.execution: cloud`).

Limits (`router.cloud`):

- **Data classes as a declaration**, not a content classifier: `routing.data_class`, else `clients.<c>.data_class`, else
  `default_data_class` (**personal**). Cloud only up to `max_cloud_data_class` (**internal**). Consequence: whoever declares nothing
  never goes to the cloud. Exception: an [institutional provider](#institutional-providers) takes its own limit.
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
- **Budget** per provider and month (`budget_month`), in the currency of the catalogue prices (`price_per_m`, per million
  tokens); exhausted = the provider drops out as a tier, from `warn_at_percent` on a Home Assistant problem is raised. Cost
  per request in usage and metrics. See [currency](#currency-of-prices-and-budgets).
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

**Turning a cloud model's reasoning off** (router 0.33.0, [ADR-0098](adr/0098-a-cloud-model-names-the-value-that-turns-its-reasoning-off.md)):
for a catalogue entry with `reasoning: true`, a client's `think: false` becomes `reasoning_effort` in the request to an
`openai` provider; `think: true` or no `think` sends nothing and leaves the provider's default. `reasoning_off` names the
value: `minimal` (the default, OpenAI's gpt-5 models) or `none` (servers that ignore `minimal`, for example a reasoning
model served by vLLM). The configuration check refuses other values, and the key without `reasoning: true` or on a local
model.

```yaml
models:
  "example-llm:reasoner-small":
    cloud: example-llm
    provider_model: example-org/reasoner-small
    reasoning: true
    reasoning_off: none
```

To find the value a model needs, send it a request with `think: false` and a small `num_predict` (64): if the answer
ends with `done_reason: length` and empty content, or `eval_count` is far above the length of the answer, the model still
thinks and needs `none`. The provider's thought (`reasoning_content`, or `reasoning` as vLLM sends it) arrives as
Ollama's `thinking` (`reasoning` in the OpenAI shape, a thinking block in the Anthropic shape), except for a client that
sent `think: false`.

### Currency of prices and budgets

Router 0.37.0, [ADR-0105](adr/0105-currency-neutral-cost-keys.md): the router never converts currencies. Prices
(`models.<m>.price_per_m`), budgets (`router.cloud.providers.<p>.budget_month`), spend and usage costs are all in the
currency you write the prices in. `router.cloud.currency` (default `CHF`, three capital letters, also in the web UI) names
it for display: the web UI shows it with every amount, the provider snapshot carries `currency`, and `/metrics` has
`skirnir_cloud_currency_info`. Changing it converts nothing; change the prices and budgets with it.

```yaml
router:
  cloud:
    currency: EUR
    providers:
      openai: { kind: openai, base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY, budget_month: 20 }
models:
  "openai:gpt-5-mini": { cloud: openai, price_per_m: { input: 0.25, output: 2.0 } }
```

- **Old names.** Up to router 0.36 the keys were `budget_month_chf` and `price_chf_per_m`. They stay accepted with the same
  meaning in `config.yaml`, in `roles.yaml` (catalogue entries and the web UI's setting
  `router.cloud.providers.<p>.budget_month_chf`) and in `PUT /admin/config`. The web UI writes the new names, so a stored
  old name migrates with the next save.
- **Both names in one place:** the same value is accepted; different values are refused, and the message names both keys.
  Across files there is no conflict: a `roles.yaml` value overrides `config.yaml` whichever name each uses.
- **Field and metric names** keep the suffix `_chf` (`spend_month_chf`, `budget_month_chf`, `cost_chf`,
  `skirnir_cloud_*_chf*`), so dashboards, alerts and Home Assistant entities keep working; their values are in the
  configured currency. The Home Assistant cost sensor still has the unit `CHF`.

### Institutional providers

Router 0.34.0, [ADR-0099](adr/0099-institutional-providers-take-their-own-data-class-limit.md):
a model server run by your own organisation (a university or company cluster), cleared under its data protection rules
for personal data. Configure it like any OpenAI-compatible provider and add `trust: institutional` with its own
`max_data_class`:

```yaml
router:
  cloud:
    egress_allow: [llm.example.org]
    providers:
      institution:
        kind: openai
        trust: institutional          # config.yaml only, not editable in the web UI
        max_data_class: personal      # replaces max_cloud_data_class for this provider
        base_url: https://llm.example.org/v1
        api_key_env: INSTITUTION_API_KEY
        secrets_file: /etc/skirnir-router/secrets.env
        budget_month: 0
```

- Its own `max_data_class` **replaces** `max_cloud_data_class` for this provider. It may be any class below the highest,
  so `secret` never leaves the house. With `trust: cloud` (the default) a provider's own class only narrows the global
  limit. A higher value has no effect, and the router logs a warning at start.
- Without its own `max_data_class` an institutional provider gets the global limit like any other.
- Everything else stays: `routing.execution: local`, `clients.<c>.cloud: false`, memory projects with `allow_cloud: false`
  and managed sessions keep requests away from it. The credential scan, HTTPS, the egress allowlist, budget and circuit
  breaker apply.
- The content check (`privacy_check`) runs only for providers whose limit is below the request's class. An
  institutional provider cleared for `personal` gets the request without the check and without redaction.
- A data class says how sensitive data is, not whose it is. Put the institutional model in its **own role**, granted
  only to the clients whose data the organisation may process (`clients.<c>.roles`). Do not add it as a fallback tier to
  roles that household or private clients use.
- `/admin/state` and the cloud table of the web UI show each provider's `trust` and its effective limit (`ceiling`).
- Use the organisation's HTTPS endpoint. A plain-HTTP address inside its network is refused like any other HTTP
  provider.
