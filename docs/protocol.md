# Router–agent protocol

How a node agent talks to the router: what to implement to write an agent of your own (for example for a platform the Go
agent does not support), and what the router and the Go agent promise each other. This is protocol **version 1**: the
protocol as router 0.37 and agent 0.36 speak it. Why it is specified this way: [ADR-0107](adr/0107-versioned-router-agent-protocol-with-a-written-specification.md).

The machine-readable part lives in [`docs/protocol/`](protocol/): one JSON Schema per message and
[`index.json`](protocol/index.json) with the frame codes, the agent's passthrough allowlist and the limits. Where this
page and a schema disagree, the code is the truth and the conformance tests are broken; please report it.

## Overview

```
agent                                    router (control port, /v1/tunnel)
  | -- WebSocket upgrade (TLS) --------------> |
  | <------------- text  {"t":"challenge"} --- |
  | --- text {"t":"hello", signed} ----------> |   identity = SHA-256 of the Ed25519 public key
  | <------------- text  {"t":"status"} ------ |   pending | approved (+ configuration) | revoked
  | --- binary HB (every 3 s) ---------------> |
  | <-------------------------- binary HBACK - |
  | --- binary OLLAMA (on change, >= 30 s) --> |
  | <---------------- binary REQ (stream n) -- |   an Ollama or OpenAI call from a client
  | --- binary RESP, DATA..., END (stream n) > |
  | <------------- text control messages ----- |   config, update, ollama-update, ollama-swap, upstream-*
```

The agent opens the connection, never the router: a node needs no inbound firewall rule, no certificate and no fixed
address ([ADR-0008](adr/0008-outbound-tunnel-from-the-agent.md)). One connection carries the login, the node's facts,
control messages and all inference requests as multiplexed streams.

## Transport

- WebSocket to `<router control URL>/v1/tunnel` (`https://` becomes `wss://`). The router serves it on its control port;
  agents verify the router certificate (system trust store or the agent setting `router.ca_file`, ADR-0070).
- Text frames carry JSON objects with a type field `t`: the login and the control messages.
- Binary frames carry `type (1 byte) | stream id (4 bytes, big endian) | payload`.
- The router pings every 20 s; the agent answers pongs and treats 90 s without any frame as a dead connection.
- Size limits: the hello is at most 32 KiB; the router accepts frames up to 2 MiB (the Go agent sends response bodies in
  pieces of at most 32 KiB); the Go agent accepts frames up to 64 MiB (a request body travels in one REQ frame).
- Reconnect: the Go agent retries after 1, 2, 5, 10, then every 30 s, and every 5 minutes after `revoked`.

## Login

1. The router sends `{"t":"challenge","nonce":<base64 of 32 random bytes>,"router":<version>}`
   ([challenge](protocol/challenge.schema.json)). The agent waits at most 15 s and refuses a nonce under 16 bytes.
2. The agent answers within 15 s with the hello ([hello](protocol/hello.schema.json)):
   `{"t":"hello","node":<name>,"pubkey":<base64>,"sig":<base64>,"facts":{...},"agent":<version>}`.
   `sig` is the Ed25519 signature over the raw bytes `nonce || UTF-8(node) || pubkey`. The router cuts `node` to
   64 characters before verifying, so a longer name never verifies. The agent keeps its key across restarts; a new key is
   a new node that the operator has to approve ([ADR-0009](adr/0009-agent-identity-by-key-and-operator-approval.md)).
3. The router answers with `{"t":"status","state":"pending"|"approved"|"revoked",...}`
   ([status](protocol/status.schema.json)). With `approved` comes the [configuration bundle](protocol/config-bundle.schema.json)
   (heartbeat interval, node name, MQTT access if configured).

Refusals: a malformed hello or a wrong signature closes the connection with code 4001 and no status. A new identity
beyond the enrollment limits (32 pending identities, 6 new ones per address in 5 minutes, ADR-0041) gets
`status pending` with a message and close code 1008. A revoked identity gets `status revoked` and is closed.

While `pending` the connection stays open: the router answers heartbeats with `state: pending` and drops OLLAMA and POWER
frames. On approval the router sends `status approved` with the bundle on the same connection.

## Facts

The hello's `facts` object describes the machine ([facts](protocol/facts.schema.json)): host name, OS and architecture,
GPU and VRAM, the MAC address for Wake-on-LAN, versions, and flags for optional features. The router stores it and
builds the node from it. No fact is required, but an agent should send `hostname`, `os`, `arch` and `agent_version`;
without the last three the router cannot offer agent updates. Feature flags (`ollama_managed`, `ollama_swap_gate`,
`upstream_release`, `upstream_fallback`) are opt-in: the router only sends the related control messages when the flag is
`true`. `gpu_present: false` (agent 0.36.0) announces a node without a GPU: the router sizes its models by main memory
instead of VRAM (ADR-0102); an absent `gpu_present` counts as a GPU, as every older agent had one.

## Frames

| Code | Name | Direction | Stream | Payload |
|---|---|---|---|---|
| 1 | REQ | router → agent | n | [request head](protocol/request-head.schema.json) JSON, newline, body |
| 2 | RESP | agent → router | n | [response head](protocol/response-head.schema.json) `{status, headers}` |
| 3 | DATA | agent → router | n | response body bytes, one frame per piece |
| 4 | END | agent → router | n | empty: the response is complete |
| 5 | ERR | agent → router | n | UTF-8 text: the upstream failed |
| 6 | CANCEL | router → agent | n | empty: abort the request |
| 7 | HB | agent → router | 0 | [heartbeat](protocol/heartbeat.schema.json) |
| 8 | HBACK | router → agent | 0 | [heartbeat answer](protocol/heartbeat-ack.schema.json) |
| 9 | OLLAMA | agent → router | 0 | [upstream state](protocol/ollama-state.schema.json) (agent 0.14.0) |
| 10 | POWER | agent → router | 0 | [power notice](protocol/power.schema.json) (agent 0.24.0) |

Both sides ignore frames shorter than five bytes and stream frames for a stream they do not know. The Go agent ignores
frame types it does not know; the router treats an unknown frame type on an open stream as a failure of that stream.

### Request streams

The router numbers streams from 1 upwards (wrapping back to 1, never 0) and sends the whole request in one REQ frame. The
agent answers with exactly one RESP, then any number of DATA frames, then END; or with ERR at any point, which fails the
request. If the first frame of a stream is not a readable RESP, the router fails the request. When the client goes away or
the deadline passes, the router sends CANCEL and forgets the stream; the agent stops the upstream request and does not
report the abort as ERR. Streams run in parallel.

The agent decides itself what it forwards, so a compromised router cannot use the node's upstream for anything else
(ADR-0073):

- A path configured as a tunnel route (agent setting `tunnel.routes`, exact path without query) goes to its local
  service, with the listed methods only; another method gets `405` with `Allow`.
- Every other request goes to the upstream only if method and path (exact, without query, no case folding) are on the
  passthrough allowlist in [`index.json`](protocol/index.json): inference, embeddings, model listing and versions.
  Everything else, model management above all, gets `403`.

The agent's own refusals are a RESP with `403` or `405`, one DATA frame with
[`{"error":{"reason":...}}`](protocol/node-refusal.schema.json) and END.

### Heartbeat

The agent sends HB every `heartbeat_interval_s` (3 s until the bundle says otherwise); the router answers each one with
HBACK (`state` free, busy or offline, `busy_reason`, `gpu_guard_ack`). The heartbeat carries `gpu_present` and, on a
node with a GPU, GPU utilisation and VRAM; as optional blocks it carries sensors, the GPU guard, update progress, the
agent's children's VRAM, a running game, main memory, the health of tunnel routes and the standby Ollama's models. The
game block says `enabled: false` when game detection is switched off on the node (agent 0.36.0, ADR-0103); `via` is
`path` or `name` (older agents sent the German `pfad`, which the router still accepts). A missing block means the agent
does not report that fact; the router then keeps its previous behaviour for it. A heartbeat the router cannot apply is
answered with `state: ""` and `error`. Since router 0.42.14 (ADR-0137) the router uses a GPU number only within
the schema's range, and free VRAM and the Ollama process only when they are not above the total; a value outside is ignored
(unknown, as without GPU), the rest of the heartbeat applies, and the node view lists the ignored fields.

Since agent 0.37.0 (ADR-0148) the heartbeat carries `ollama_runners: {count, private_mib}` when Ollama is the agent's child
on Windows: the processes in Ollama's process tree besides the server and its console host, one runner per loaded model,
and their private working sets in MiB. `count` 0 means no runner lives, so an unloaded model's main memory is released;
after `keep_alive: 0` that comes seconds later than the empty `/api/ps`. The block is absent where the agent cannot tell
(Ollama not its child or not running, Linux). The router (0.44.0) drops a block outside the schema as implausible and
uses the block only while the heartbeat is fresh.

### Upstream state

With OLLAMA the agent reports what its upstream holds: `up`, the entries of `/api/tags` and `/api/ps` unchanged, and for an
OpenAI-compatible upstream whether it is held, starting, blocked or replaced by the standby Ollama. It sends the frame at
the start of each session, on every change, after approval and at least every 30 s. While a report is younger than 45 s
the router does not poll the node itself. `up: false` takes the node out of routing at once.

### Power

POWER says `suspending` (the router takes the node out of routing at once) or `resumed`. The agent repeats its latest
notice at the start of each session, before the first heartbeat.

## Control messages

After the login the router may send these text messages at any time; the agent ignores types it does not know.

| `t` | Schema | Sent when |
|---|---|---|
| `status` | [status](protocol/status.schema.json) | approval or revocation of a connected identity |
| `config` | [status](protocol/status.schema.json) | the node's configuration bundle changed |
| `update` | [control-update](protocol/control-update.schema.json) | agent update; the agent trusts only the operator's signature over the manifest ([ADR-0020](adr/0020-signed-agent-updates-through-the-router.md)) |
| `ollama-update` | [control-ollama-update](protocol/control-ollama-update.schema.json) | Ollama update for `ollama_managed` nodes; the router chooses the version, the agent the source ([ADR-0021](adr/0021-ollama-as-agent-child-updated-through-the-router.md)) |
| `ollama-swap` | [control-ollama-swap](protocol/control-ollama-swap.schema.json) | release of a downloaded Ollama update on an idle node (`ollama_swap_gate`) |
| `upstream-hold`, `upstream-release` | [control-upstream](protocol/control-upstream.schema.json) | stop or start the OpenAI-compatible upstream (`upstream_release`) |

## Compatibility rules

- **Unknown fields are ignored** by both sides; new optional fields are how the protocol has grown so far. Each schema
  field says since which agent version it exists (`x-since-agent`; `baseline` means in every agent of this repository).
- **Optional features are announced** by the agent as facts and used by the router only when announced.
- **Required** in a schema means the receiver refuses or drops the message without the field. Everything else is optional
  and has a documented meaning when absent.
- Fields marked `x-legacy` are read by the router but not sent by the current agent; `x-ignored-by-receiver` marks
  informational fields; `x-open` marks objects defined elsewhere (Ollama's model entries), which `--strict` does not
  check field by field.
- Free-text fields (`message`, `error`, ERR payloads) are for logs and display only. Decisions use the fixed fields and
  codes, never the text ([ADR-0023](adr/0023-explicit-control-flow-and-error-codes.md)).

A protocol version number and a capability list are not part of version 1; ADR-0107 describes them as the next step.

## Security

- The router authenticates the agent by its Ed25519 key and the operator's approval; the agent authenticates the router by
  TLS. Everything after the login is trusted only as far as the identity is approved.
- The router treats every agent message as untrusted input: size limits, type checks, and a malformed heartbeat or report
  is answered or dropped without ending the tunnel.
- The agent treats the router as untrusted for what it executes: the passthrough allowlist, signed agent updates, Ollama
  archives from the agent's own source, refused downgrades.
- The configuration bundle and the update token are secrets: agents must not log them.

## Checking an agent

`python test/protocol_schema.py <schema> <message.json> [--strict]` validates a captured message against a schema
(`--strict` also refuses undocumented fields). The validator needs no packages beyond Python and implements only the
JSON Schema keywords these files use.

The conformance tests keep the specification and the implementations in step:

- `test/selftest_protocol.py` checks the schemas and their examples, compares frame codes and limits with the router and
  `test/fake_agent.py`, checks that every control message the router builds and every field it reads is documented, that
  "required" holds in the router, and runs the real router against the fake agent through a relay that validates every
  message in both directions.
- `agent/protocol_conformance_test.go` compares the Go agent's message types and heartbeat keys with the schemas in both
  directions, decodes every documented router message into the agent's types, compares frame codes and the passthrough
  allowlist with `index.json`, and runs the real login against a fake router.

A change to the protocol changes `docs/protocol/`, this page and its German twin in the same pull request; the tests
fail until all three sides agree.
