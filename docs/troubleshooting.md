# Troubleshooting

**English** · [Deutsch](troubleshooting.de.md) · [README](../README.md)

Start with the logs and the posture:

```bash
journalctl -u skirnir-router -f                                         # router log; posture findings at start (lines "posture …")
python3 /opt/skirnir-router/router.py --check /etc/skirnir-router/config.yaml --files
python3 /opt/skirnir-router/router.py --posture /etc/skirnir-router/config.yaml
```

Every response carries `X-Skirnir-Request-Id`; the same id is in the decision log (`/admin/state`, web UI *Overview*) and,
for refusals, in the audit log (`client_auth.audit_log`, e.g. `/var/log/skirnir-router/audit.jsonl`).

## Control port (11435)

| Symptom | Cause and fix |
|---|---|
| **503** on every page, also from loopback | No `control_auth.users`. Run `router.py --init` for a new installation, or add a user with a hash from `router.py --hash` ([ADR-0053](adr/0053-control-port-requires-users-even-on-loopback.md)). |
| **401** | Wrong user or password. After repeated failures from one address the router answers **429** for a minute. |
| **403** "CSRF protection" | A non-GET request to `/admin/*` without the header `X-Skirnir-UI: 1` ([ADR-0042](adr/0042-admin-writes-require-a-custom-header.md)); scripts must send it, the web UI does. |
| Not reachable from another machine | `control_listen` is unset (default `127.0.0.1:11435`) or a firewall blocks 11435. |

## API port (11434)

| Status | Cause and fix |
|---|---|
| **401** | Under `client_auth.mode: enforce` the request has no valid identity: no token, a wrong token (audit `auth_denied`, counted as `bad_token`; a wrong token never falls back to the source IP), or an address not listed. Check the header (`Authorization: Bearer <token>`, or `x-api-key` for Anthropic clients). |
| **403** | The client is known but not allowed: role not in `roles`, a concrete model name without `models: true`, or `inference: false` (audit `forbidden`). |
| **429** | `requests_per_minute` exceeded (audit `rate_limited`) or more than `max_concurrent` requests in progress (audit `concurrency_limited`, `Retry-After: 1`). Raise the limit for that client. |
| **413** | Body above the client's `max_body_mib` (default 16, audit `body_too_large`), or above the global limits (`max_images` 16, `max_tools` 128, `max_messages` 1000, body 64 MiB). |
| **400** with a plain-text reason | No tier of the role has a capability from `routing.require`. |
| **404** | Unknown role or model, or a concrete cloud model name (only callable through roles). |
| **503** with a reason code | No capacity: no node online, `gpu_busy`, `vram_full`, `memory_pressure` (`Retry-After` 60 s), `server_busy` (`Retry-After` 30 s), queue full (`max_queue`) or waiting budget exceeded. Anthropic clients get **529** `overloaded_error` instead. |
| **424** `rerank_unavailable` | No node can rerank right now ([reference.md](reference.md#optional-rerank-endpoint)). |
| **501** on `/v1/embeddings` | The model is a chat model; embeddings need an embedding model on the node. |

In observe mode unknown callers are served and logged as `auth_missing`; switch to `enforce` once every client has its token.

## GPU nodes and agents

| Symptom | Cause and fix |
|---|---|
| Agent stays *pending* | Not approved yet. Compare the fingerprint with `skirnir-agent identity` on the node (administrator/root), then approve in the *Agents* tab. |
| Agent does not appear at all | Tunnel fails: check `skirnir-agent status`, the agent log (Windows `C:\ProgramData\skirnir-agent\logs\agent.log`, Linux `journalctl -u skirnir-agent`), the router address in the configuration and the firewall for 11435. |
| TLS or certificate errors on an agent | The node does not trust the router's certificate. Use a public certificate, or set `router.ca_file` to the private CA ([install-agent.md](install-agent.md#trusting-the-routers-certificate)); `skirnir-agent check-config` refuses an unusable CA file. |
| TLS errors in Home Assistant or other clients | Same cause: the client must trust the certificate on 11434 (public certificate, or the private CA in the client's trust store). |
| Agent refuses its configuration after an update | An unknown top-level key; `skirnir-agent check-config` names it ([ADR-0051](adr/0051-agent-rejects-unknown-top-level-configuration-keys.md)). |
| Update refused, "invalid manifest signature" | The node does not trust the signing key; add it with `Set-UpdateKey.cmd` or `update.public_keys` ([operations.md](operations.md#key-rotation)). Kind `downgrade`: the release is older than the running agent. |
| Node shows no models / role not servable | Ollama not reachable on `127.0.0.1:11434` or the role's models not pulled on that node. |
| Node stays `busy` | A game or foreign GPU load; `skirnir-agent game` shows game detection, `skirnir-agent gpu` the sensors, `/admin/state` `game_heavy` and its reason. |
| Model slow | Part of it is in system RAM: Home Assistant `binary_sensor.skirnir_node_<node>_ram_spill` ([operations.md](operations.md#home-assistant-entities)). |

Agent commands for diagnosis: `skirnir-agent check-config` (configuration), `status` (service and `/health`), `gpu`
(measurement source and sensors), `identity` (fingerprint), `game` (game detection and memory).

## Memory states

Requests with memory recall carry `X-Skirnir-Memory-State` ([design/hindsight-memory.md](../design/hindsight-memory.md)):

| State | Meaning |
|---|---|
| `available` | Memory answered; no facts means no matching knowledge. |
| `bank_missing` | Nothing was ever retained under this scope and namespace key (also after a namespace key change). |
| `skipped:tools` | The request carries tools and the project grant does not set `recall_into_tools: true` ([ADR-0059](adr/0059-no-automatic-recall-into-requests-with-tools.md)). Other `skipped:<reason>`: `no_messages`, `last_not_user`, `content_not_text`. |
| `query_filtered` | The sensitive-data filter kept the query from being sent; explicit recall answers 403. |
| `unavailable`, `unavailable:<kind>` | No usable answer (`timeout`, `transport`, `protocol`, `upstream_auth`, `upstream_rejected`). Inference proceeds without facts with `fail_open: true`, else 503. |

A client that does not read the header still shows up when its recalls are skipped for tools, for example a Home
Assistant entry after a tool API was enabled on it ([ADR-0090](adr/0090-report-recall-skipped-for-tools-per-client.md)):
the Overview and `/admin/context` (`recall_skipped_for_tools`) name the client, its project, since when and how many
requests, until an hour after its last such request. `skirnir_memory_recall_skipped_for_tools_clients` counts these
clients, and the log says once per client and hour that it happened, without names. If the client should get the
facts, set `recall_into_tools: true` on its grant, which accepts that facts reach a model that can call tools; otherwise
turn its recall off or leave it.
