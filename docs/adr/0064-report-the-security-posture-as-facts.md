---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Report the security posture as facts

> Source: TM-01 and TM-11 in the threat model.

## Context and Problem Statement

The threat model asks the operator to run with `enforce`, a locked policy, separate tokens and TLS (TM-01), and to keep
backends reachable only through the router (TM-11). Whether a deployment does so was only visible by reading the
configuration line by line. A setting left in observe mode, a token copied to a second client or a node's Ollama reached
over plain HTTP stays unnoticed until it matters.

## Decision Drivers

* The router knows its configuration; it should state what that configuration allows, instead of the operator inferring it.
* No finding may print a secret, a token hash or a connection string.
* A risky but deliberate setting must not stop the router; it must stay visible.

## Considered Options

* `router.py --posture <config>` plus the same report in the start log: findings with code, severity, risk and next step
* Refuse to start on risky settings
* Document a manual checklist

## Decision Outcome

Chosen option: "Report as facts on demand and on every start", because the operator decides what to accept while nothing
risky stays silent. Refusing to start would break deliberate setups (a rerank-only client identified by address, a
development loopback); a checklist is the weakest protection.

* Findings: `client_auth_observe` (high), `client_auth_unlocked`, `client_ip_only` (medium; info when the client has
  `inference: false`), `clients_share_token` (high), `control_without_users` (high), `control_open_loopback`,
  `api_without_tls`, `node_plain_http` (static node over HTTP without fingerprint, not loopback),
  `session_store_unverified_tls` (DSN `sslmode` below `verify-ca` to a non-loopback host; read from the environment, never
  printed), `session_store_dsn_unset` (info).
* `--posture` exits 0 with findings and 2 only for an invalid configuration; the last line is `POSTURE: ok` or the counts.
* The agent example configuration binds a child Ollama to loopback; the agent reaches it there, and an address on all
  interfaces made Ollama reachable around the router.

### Consequences

* Good, because every start states the deployment's exposure in the log, and a check can run before a rollout.
* Good, because accepted risks stay visible instead of forgotten.
* Bad, because the report sees only the router's configuration: a backend's own listening address or egress (Hindsight,
  PostgreSQL, a node's Ollama) needs an inspection on that host.
* Neutral, because findings do not change behaviour.

### Confirmation

`test/selftest_posture.py`: each finding from a synthetic configuration, a clean configuration reports `POSTURE: ok`, no
token hash or DSN in the output, and `router.py --posture` as a process.
