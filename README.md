# Skirnir – self-hosted LLM router and AI gateway for Ollama

**English** · [Deutsch](README.de.md)

<img src="design/skirnir-logo.png" alt="Skirnir" width="120" align="right">

Skirnir ([psewar/skirnir](https://github.com/psewar/skirnir)) is a self-hosted LLM router and AI gateway for Ollama,
with OpenAI-compatible and Anthropic Messages APIs. It provides GPU-aware model routing across multiple nodes,
optional cloud fallback, and optional persistent session context and shared agent memory.

Skirnir sits in front of one or more Ollama installations on GPU machines and looks to its clients like **a single** Ollama
server. Clients do not ask for a specific model but for a **role** (`standard:latest`, `gross:latest`, `code:latest` …; role
names are yours to choose, the example configuration uses German ones, `gross` = large). For each request the router picks
node, model and context size based on what is loaded right now, whether the GPU is occupied by a game, how much VRAM is free,
and how fast and how error-free a model was on a node recently. The Ollama API stays unchanged, plus there is an
OpenAI-compatible interface under `/v1` and the Anthropic Messages API (`/v1/messages`, e.g. for Claude Code).

Built for a homelab: Home Assistant, Node-RED and local agent frameworks as clients, Windows gaming PCs as GPU nodes that are
not running around the clock and do not belong to the AI alone. The name: in Norse mythology Skirnir is Freyr's messenger, who
rides into foreign realms, negotiates there and returns with the answer.

**Status:** router 0.23.0, agent 0.30.0 (see [releases](https://github.com/psewar/skirnir/releases) and
[CHANGELOG.md](CHANGELOG.md), which maps tags to router and agent versions), in continuous operation in one homelab since
September 2026, one developer. No stability guarantees. The documentation exists in English and German; the sub-READMEs
(agent, decision engine) are in German. If you want to run it, you should know Python, systemd and Ollama.

Security development: [threat model](docs/security/threat-model.md), [risk register](docs/security/risk-register.json)
and [pre-PR review/CI guide](design/continuous-security-review.md). Every PR needs a fresh change-bound review record;
optional model reviews are advisory and require an explicitly configured endpoint.

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
- **Cloud as a tier.** OpenAI, Anthropic and other OpenAI-compatible providers (for example Mistral) can sit as the last (or
  first) tier of a role, with data classes, credential scan, monthly budget and an egress allowlist. By default nothing goes to
  the cloud.
- **Client identities** on the inference port (Bearer, Basic, source IP), role and model locks, rate limit, audit log.
- **Observable:** Prometheus endpoint, usage per day and client, decision log, Home Assistant device via MQTT discovery, web UI
  with live charts, catalogue, role editor, playground and load test.
- **Operations-ready:** config schema with suggestions on typos, idempotency key, canary and shadow tiers per role, deploy manifest
  with SHA-256, Ollama versions and model digests per node.
- **TLS on every link** once configured (see [security essentials](#security-essentials)), one process, one event loop, no
  database. Dependencies: `aiohttp`, `pyyaml`, `cryptography`, optionally `paho-mqtt` and `uvloop`.

## Architecture

```
  Clients: Home Assistant · Node-RED · OpenAI-compatible tools · Claude Code · agent frameworks
      │  https :11434   Ollama API (/api/*), OpenAI API (/v1/*), Anthropic API (/v1/messages), client identity
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

The agent reports its node's state itself. Every 2 s it sends GPU utilisation, VRAM and sensors (heartbeat). Since agent
0.14.0 it also watches its local Ollama and sends the installed models (`/api/tags`), the loaded models (`/api/ps`) and whether
Ollama answers at all, immediately on every change and in full at least every 30 s. From both the router derives the per-node
state `offline` / `free` / `busy`. Only for nodes whose agent does not report Ollama's state (older agents, static nodes without
an agent) does the router still ask Ollama itself every 5 s. All Ollama calls made by the router go through the agent's tunnel;
Ollama itself stays on `localhost`. Ports and endpoints: [docs/reference.md](docs/reference.md#interfaces).

## Quick start

**1. Try it without a GPU.** From a clone, with Python 3.11+ and the packages in `requirements-dev.txt`:

```bash
python test/run_tests.py     # once: creates the self-signed test/test-cert.pem the fake node needs (needs openssl, ~3 min)
python test/dev_env.py       # fake nodes, fake cloud, fake agent and the router; open http://127.0.0.1:21435/
```

No TLS and no login in this environment; the fake agent `big` waits for approval in the *Agents* tab.

**2. Install the router** on Debian or Ubuntu (Python 3.11 or newer), from a clone or release:

```bash
sudo bash router/install.sh
sudo python3 /opt/skirnir-router/router.py --init /etc/skirnir-router --public-url https://<name>:11435
```

`--init` asks for the web UI password (at least 12 characters), generates one client token and shows it **once**, and writes
a safe configuration with the role `standard`. Put the TLS certificate and key for `<name>` at the paths in
`router.control_tls` (default `/etc/skirnir-router/certs/fullchain.pem` and `privkey.pem`), then
`sudo systemctl enable --now skirnir-router` and open `https://<name>:11435/` (user `admin`). Details, TLS options and the
firewall: [docs/install-router.md](docs/install-router.md).

**3. Add a GPU node.** Install Ollama and keep it on loopback (`OLLAMA_HOST=127.0.0.1`), then pull the model of the role:
`ollama pull qwen3:8b`. Download the agent from the [release](https://github.com/psewar/skirnir/releases):

- Windows: double-click `skirnir-agent-<version>-windows-amd64.exe`, confirm UAC, answer three questions (router address
  `https://<name>:11435`, the operator's update key or empty, the user allowed to control the service).
- Linux: `chmod +x skirnir-agent-<version>-linux-amd64 && sudo ./skirnir-agent-<version>-linux-amd64 setup --router https://<name>:11435`

The node must trust the router's certificate (a public one, or a private CA in `router.ca_file`). Details:
[docs/install-agent.md](docs/install-agent.md).

**4. Approve the node.** It appears as pending in the web UI, tab *Agents*. On the node run `skirnir-agent identity` (as
administrator or root) and approve only if the fingerprint matches the one in the UI.

**5. First request** with the client token from step 2, OpenAI-compatible:

```bash
curl https://<name>:11434/v1/chat/completions -H "Authorization: Bearer <client token>" -H "Content-Type: application/json" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}]}'
```

or native Ollama:

```bash
curl https://<name>:11434/api/chat -H "Authorization: Bearer <client token>" \
  -d '{"model":"standard:latest","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

The first request loads the model and takes longer. With `curl -i` the headers `X-Skirnir-Node`, `-Model` and `-Tier` show
where it ran.

**6. Connect Home Assistant.** Add the *Ollama* integration with URL `https://<name>:11434/` and the client token as API key;
the model is the role name (`standard:latest`). Give Home Assistant its own client and token (web UI, tab *Clients*). More
clients: [docs/clients.md](docs/clients.md).

## Concepts

- **Roles and tiers.** A client asks for a role; the role lists tiers (model and context size) in order of quality. The router
  picks the best tier a node can serve now and prefers a model that is already loaded.
  [Configuration](docs/configuration.md#roles-and-tiers).
- **The client describes, the router decides.** An optional `routing` block names required capabilities, minimum context,
  priority, session and data class; the router chooses node, model and tier. [Clients](docs/clients.md#the-routing-block).
- **Busy detection and game mode.** A game or other foreign GPU load makes a node `busy`: only `busy_ok` tiers run there,
  large models are unloaded and prewarmed again afterwards. [Configuration](docs/configuration.md#roles-and-tiers).
- **Cloud as a tier.** A cloud model is just another tier of a role, guarded by data classes, a credential scan, a budget and
  an egress allowlist; off by default. [Configuration](docs/configuration.md#cloud-as-a-tier-in-roles).
- **Auto role.** `auto:latest` lets a chain of local engines pick the role. [Configuration](docs/configuration.md#auto-role-the-decision-engine).
- **Memory and sessions (optional).** Session store, Hindsight memory, managed context and shared memory are off by default.
  [Reference](docs/reference.md#optional-persistent-session-api).

## Security essentials

- `router.client_auth.mode: enforce` and `locked: true`, so unknown callers get 401 and the mode cannot be switched in the UI
  (`router.py --init` writes both).
- One token per client; never keep an example token. Tokens are stored only as sha256.
- TLS on both ports: `router.control_tls` (cert, key) for the control port 11435 and `router.api_tls: true` for the API port
  11434. `api_tls` defaults to `false`; without `control_tls` the control port speaks plain HTTP.
- Firewall port 11435 to the GPU nodes and admin machines; 11434 is for clients.
- Ollama on the nodes bound to loopback (`OLLAMA_HOST=127.0.0.1`); the router reaches it only through the agent's tunnel.
- Compare the fingerprint with `skirnir-agent identity` before approving a node, over another channel than the network it
  enrolled through (TM-07).
- Run `router.py --posture /etc/skirnir-router/config.yaml` before every rollout; the start log repeats the findings.
- The router runs as its own account `skirnir-router` **without capabilities**
  ([ADR-0086](docs/adr/0086-router-runs-as-its-own-account.md)): it reads its configuration and TLS key through its group
  and writes only `/var/lib/skirnir-router` (`router.state_dir`) and its logs. `install.sh` and `deploy.py` set this up
  (`router-layout.sh`); `router.py --check <config> --files` checks the files as that account.
- Never pass passwords on the command line: `router.py --hash` without an argument asks without echo.
- Whatever shifts the trust base (ports, TLS, identities, provider endpoints, egress, `client_auth.mode` when `locked`) cannot
  be changed via UI or API, only in the configuration file.
- Secrets live in `secrets.env` (root, readable for the router's group) and never appear in logs or `/admin/state`; the router has no write access to the
  secret store. Prompts go to the cloud only with a declared data class and never with recognisable keys in them.
- Agents log in over WSS with an Ed25519 challenge; MQTT runs over 8883. Ollama updates are fetched by the agent itself from a
  fixed source (`ollama_update.source`, default GitHub releases of ollama/ollama) and checked against that release's
  `sha256sum.txt`; the router only names the version.

Vulnerability reports: [SECURITY.md](SECURITY.md).

## Documentation

| Document | Content |
|---|---|
| [docs/install-router.md](docs/install-router.md) | Requirements, `install.sh`, first configuration, TLS, firewall, secrets, command reference, manual install, LXC deploy |
| [docs/install-agent.md](docs/install-agent.md) | GPU nodes on Windows and Linux, Ollama, approval, update key |
| [docs/configuration.md](docs/configuration.md) | Client identities, roles and tiers, scheduler and admission, auto role, cloud tiers |
| [docs/clients.md](docs/clients.md) | Home Assistant, Node-RED, OpenAI and Anthropic clients, Claude Code, the `routing` block |
| [docs/operations.md](docs/operations.md) | Web UI, metrics, Home Assistant entities, agent and Ollama updates, key rotation, names, measurements |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Status codes, pending agents, TLS errors, logs, memory states |
| [docs/reference.md](docs/reference.md) | Interfaces, GPU guard, optional memory and session features, rerank, OpenAI-compatible nodes, limits, repo layout |
| [docs/adr/](docs/adr/README.md) | Architecture Decision Records: why Skirnir is built this way |
| [agent/README.md](agent/README.md) | Agent internals (German) |

## Development

```bash
python test/run_tests.py          # about 250 end-to-end checks, ~3 min: two fake Ollama nodes, fake agent through the tunnel,
                                  # fake cloud (OpenAI and Anthropic), fake decision service, self-signed TLS, client auth,
                                  # admission, breaker, canary/shadow, idempotency, cloud limits, UI config round trips
python test/selftest_context_check.py   # standalone self-tests without a router: context pre-check and
python test/selftest_toolcall_rescue.py   # tool-call rescue (a few seconds each)
python test/dev_env.py            # the same environment for clicking around, without TLS/login
python test/perf_run.py           # test bench: CPU per request, py-spy profile (test/perf_profile_report.py)
ruff check .                      # lint (ruff.toml: F, E9, B904, B905)
```

Dependencies for running and developing: `router/requirements.txt` (runtime, matches the Debian 12 packages) and
`requirements-dev.txt` (lint, deploy, tests). CI runs lint, `go vet`/`go test`, a Windows cross-build and the self-tests on
every push; the end-to-end suite runs nightly because it is timing-sensitive. Workflow, PR requirements and project rules:
[AGENTS.md](AGENTS.md); release history: [CHANGELOG.md](CHANGELOG.md); repo layout: [docs/reference.md](docs/reference.md#repo-layout).

## License

[MIT](LICENSE). The models Skirnir distributes and the third-party projects involved (Jevlike, multilingual-e5-small, Ollama)
have their own licenses.
