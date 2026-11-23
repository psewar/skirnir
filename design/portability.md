# Portability roadmap

> Status: proposed (2026-11-23). Decisions (why): [ADR-0109](../docs/adr/0109-status-sinks-instead-of-a-fixed-home-assistant-publisher.md),
> [ADR-0110](../docs/adr/0110-a-backend-protocol-for-ollama-nodes-openai-compatible-servers-and-cloud-providers.md),
> [ADR-0111](../docs/adr/0111-the-agent-talks-to-its-inference-server-through-a-runtime-interface.md),
> [ADR-0112](../docs/adr/0112-the-agent-probes-gpus-through-an-interface-and-reports-a-list-of-devices.md),
> [ADR-0113](../docs/adr/0113-busy-detection-is-a-set-of-signal-sources-that-can-be-switched-off.md),
> [ADR-0114](../docs/adr/0114-the-session-store-is-chosen-by-its-connection-url.md),
> [ADR-0115](../docs/adr/0115-long-term-memory-goes-through-a-memory-backend-interface.md),
> [ADR-0116](../docs/adr/0116-an-official-container-image-for-the-router.md),
> [ADR-0117](../docs/adr/0117-waking-nodes-and-fetching-ollama-releases-go-through-replaceable-sources.md),
> [ADR-0118](../docs/adr/0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md),
> [ADR-0119](../docs/adr/0119-privacy-redaction-per-tool-format-and-locale-pack.md). All of them are `proposed`; this
> document describes the plan, not built behaviour. Done steps link the ADRs they were built under (ADR-0101 to ADR-0108).

## Why

Skirnir was built for one homelab, and its values are not hard-coded: there are no node names, model names or times in
the code, and roles, tiers, windows and cloud providers all come from the configuration. Its *concepts*, though, assume
one setup (as of router 0.34, before phase 0):

1. **A node is Ollama on an NVIDIA gaming PC.** The agent did not start without NVIDIA, game detection always ran, and
   an OpenAI-compatible server is still reported to the router as an Ollama with made-up sizes. Phase 0 fixed the first
   two (ADR-0102, ADR-0103); one GPU per node, NVIDIA only, and Ollama as the model of every upstream remain.
2. **The operator is told through Home Assistant over MQTT.** The problem list was the Home Assistant state only;
   since ADR-0101 problems can also be pulled from `/metrics` and the web UI, but the only push path is still Home
   Assistant, and every agent gets the router's broker login.
3. **Storage is PostgreSQL, memory is Hindsight, secrets come from one secret store, and the router runs on Debian with
   systemd.** Phase 0 opened the secret source (ADR-0104); the rest remains.

The goal is to open these concepts to other setups (other vendors, servers, home automation systems, single hosts,
containers) without a big rewrite and without weakening the security and supply-chain properties.

## How: seams on the existing pattern

The router already has the model for a seam: the decision engines share one interface
(`router/skirnir_router/decision/base.py`), there are several implementations, and the configuration chooses
(`router/skirnir_router/decision/__init__.py`). Every seam below follows it:

* a typed interface (Python `Protocol` or abstract class, Go `interface`);
* implementations in the repository, chosen by a fixed name in the configuration, with no dynamically loaded plugins
  ([ADR-0118](../docs/adr/0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md));
* today's behaviour as the default, so an existing configuration keeps working;
* expected conditions returned as values (`Problem`, facts), as `AGENTS.md` requires.

Each step is a pull request of its own with its tests, documentation and ADR. There is no long-lived refactoring branch.

## Phase 0: clean-up without architecture changes (done)

Small, low-risk PRs, each with its own ADR where it decided something. All of them are merged:

* done: problems are structured records (`code`, `severity`, `node`, …) and exported in `/metrics`,
  `GET /admin/problems` and the web UI ([ADR-0101](../docs/adr/0101-problems-are-structured-records-exported-as-metrics.md));
* done: the agent starts and sends heartbeats without a GPU, and the router sizes such a node by main memory
  ([ADR-0102](../docs/adr/0102-the-agent-starts-without-a-gpu.md));
* done: game detection can be switched off ([ADR-0103](../docs/adr/0103-game-detection-is-a-module-that-can-be-switched-off.md));
  the Windows event-source check no longer reads error text (agent 0.36.0);
* done: the secrets renderer checks its source and renders every key on its own, without depending on MQTT; MQTT is
  optional in `install.sh` (`--without-mqtt`)
  ([ADR-0104](../docs/adr/0104-the-secrets-renderer-checks-its-source-and-renders-every-key-on-its-own.md));
* done: locale-neutral router: currency-neutral cost keys ([ADR-0105](../docs/adr/0105-currency-neutral-cost-keys.md)) and
  language-dependent defaults such as the benchmark prompt in the configuration
  ([ADR-0106](../docs/adr/0106-language-dependent-defaults-live-in-the-configuration.md));
* done: README status line and the "what to back up" list in `docs/operations.md` (documentation only).

## Phase 1: draw the seams

| Step | What | Why | Depends on |
|---|---|---|---|
| 1.1 Status sinks | `StatusSink` with `mqtt_ha` (today), `mqtt_plain`, `webhook`, `none`; several at once; a per-node MQTT login or no MQTT in the agent bundle, the router's discovery prefix passed on ([ADR-0109](../docs/adr/0109-status-sinks-instead-of-a-fixed-home-assistant-publisher.md)) | operators without Home Assistant get the same problems; nodes stop holding the router's broker credential | ADR-0101 (done) |
| 1.2 Protocol specification | written router–agent protocol with a version and capability list; `test/fake_agent.py` becomes a conformance test. **Done** as version 1 without a version field on the wire; the field follows in its own ADR ([ADR-0107](../docs/adr/0107-versioned-router-agent-protocol-with-a-written-specification.md)) | third-party agents (for example for macOS) without reading `tunnel.go`; a base for 2.2 and 2.3 | – |
| 1.3 Pure node assessment | one assessment function for candidate selection, blocking reasons, prewarm and residency budget, without side effects. **Done**: one assessment for selection and explanation, shared budget helpers for prewarm, residency and standby ([ADR-0108](../docs/adr/0108-one-pure-node-assessment-for-selection-and-explanation.md)) | removes duplicated feasibility logic that can drift; a base for 2.1 and 2.4 | – |
| 1.4 Shape protocol | standard methods instead of `hasattr` checks on the API front-door shapes in `proxy.py`. **In review** (PR #179, ADR-0120) | smaller surface for 2.1 | – |

1.1 and 1.2 give the most to other users per effort; 1.3 and 1.4 give the most to maintainability. With 1.2 and 1.3
done, 1.1 is the next open step of phase 1.

## Phase 2: replaceable backends

| Step | What | Why | Depends on |
|---|---|---|---|
| 2.1 Router backends | `Backend` protocol: `OllamaBackend`, `OpenAIServerBackend`, `CloudBackend` with `execute(...)` returning Ollama-JSON chunks or a `Problem`; order: Ollama path to dict chunks, merge the three completion paths in `_Relay`, then replace the branch in `_Relay.run` ([ADR-0110](../docs/adr/0110-a-backend-protocol-for-ollama-nodes-openai-compatible-servers-and-cloud-providers.md)) | new target kinds without branches in five modules; in-process fake backends for tests | 1.3 (done), 1.4 |
| 2.2 Agent runtimes | `Runtime` with `ollama`, `openai-generic`, `llama-server`, `vllm`, `lmstudio`; a path allowlist per runtime ([ADR-0111](../docs/adr/0111-the-agent-talks-to-its-inference-server-through-a-runtime-interface.md)) | other inference servers as first-class nodes, reported as what they are | 1.2 (done) |
| 2.3 GPU probes | `GPUProbe` with a list of devices; NVML on Linux without cgo; multi-GPU; later ROCm, Intel, Apple ([ADR-0112](../docs/adr/0112-the-agent-probes-gpus-through-an-interface-and-reports-a-list-of-devices.md)) | AMD, Intel, Apple and multi-GPU nodes with measured memory | ADR-0102 (done), 1.2 (done) |
| 2.4 Busy signal sources | `evaluate` returns events and a dispatcher acts; then each busy signal can be switched off ([ADR-0113](../docs/adr/0113-busy-detection-is-a-set-of-signal-sources-that-can-be-switched-off.md)) | dedicated servers without the gaming-PC logic | 1.3 (done), ADR-0103 (done) |
| 2.5 Split `Node` | transport, inventory and VRAM, host signals and upstream control behind one facade | `Node` (`nodes.py`) mixes all of them; a refactoring without a decision of its own | 2.1, 2.4 |

2.4 carries the highest risk: it is the most tuned code in the router. Its first step changes no behaviour, and the
switches come only after the existing tests and the end-to-end suite pass unchanged.

## Phase 3: storage, memory, operations

Built when someone asks for it. The container image may be brought forward if Skirnir is to be offered more widely.

| Step | What | Why | Depends on |
|---|---|---|---|
| 3.1 Session store by URL, SQLite | `open_session_store(url)`, migrations per engine, SQLite for single hosts; `OutboxStore` split off ([ADR-0114](../docs/adr/0114-the-session-store-is-chosen-by-its-connection-url.md)) | sessions and reliable memory without a PostgreSQL server | – |
| 3.2 Memory backends | `MemoryBackend`; the Hindsight adapter normalises answers to `Fact` ([ADR-0115](../docs/adr/0115-long-term-memory-goes-through-a-memory-backend-interface.md)) | mem0, pgvector and others without touching the policy code | – |
| 3.3 Container image | official, reproducible router image with an SBOM, `state_dir` as a volume, secrets by environment, compose example ([ADR-0116](../docs/adr/0116-an-official-container-image-for-the-router.md)) | Docker, Unraid and TrueNAS users | ADR-0104 (done) |
| 3.4 Wakers and release sources | `Waker`: `wol`, `http_webhook`, `ipmi`; `ReleaseSource`: GitHub or a neutral release document ([ADR-0117](../docs/adr/0117-waking-nodes-and-fetching-ollama-releases-go-through-replaceable-sources.md)) | wake machines behind smart plugs or BMCs; internal mirrors | – |
| 3.5 Context redactors | `ContextRedactor` per tool format (`ha_assist`, `openhab`, `jsonpath`) and locale packs ([ADR-0119](../docs/adr/0119-privacy-redaction-per-tool-format-and-locale-pack.md)) | privacy redaction for other home automation systems and countries | – |

An agent container mode is considered after 2.2 and 2.3.

## Deliberately not

* **No big bang** and no refactoring branch that lives for weeks.
* **No dynamically loaded third-party plugins** (Python entry points, Go plugins): they would bypass the reproducible
  build, the SBOM and the threat model
  ([ADR-0118](../docs/adr/0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md)). Third-party
  adapters come as pull requests.
* **No OpenTelemetry requirement**: [ADR-0013](../docs/adr/0013-observability-without-opentelemetry.md) stays; the
  structured problems in `/metrics` (ADR-0101) cover alerting through Prometheus.
