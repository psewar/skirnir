---
status: accepted
date: 2026-09-25
decision-makers: operator
---

# Agents update themselves through the router from operator-signed releases, canary first

> Recorded retroactively on 2026-10-30; decided on 2026-09-25 (v0.4.0: router 0.1.5, agent 0.8.4). Source: README
> "Updating agents", CHANGELOG v0.4.0.

## Context and Problem Statement

Agents run on several machines, some of them used by other people. Updating each by hand does not scale, but a router
that can push code to every node would be a powerful target.

## Decision Drivers

* The router must not be able to run code of its own choosing on the nodes.
* Updates must reach nodes through their outbound tunnel (no inbound access).
* A broken version should hit one node first.

## Considered Options

* `deploy.py --agent` stores binaries with a manifest signed by an operator Ed25519 key (never by the router); agents
  verify signature and hash, swap themselves and restart; rollout loop with a canary node
* The router signs and distributes
* Manual updates per node

## Decision Outcome

Chosen option: "Operator-signed manifest, distributed by the router", because a compromised router can at most hold back
or replay versions, not sign new ones. A canary node gets new versions first; other nodes follow after it ran cleanly. On
Windows the binary is Authenticode-signed after the build where Smart App Control blocks unsigned code.

### Consequences

* Good, because a release reaches all nodes without logging in to them.
* Bad, because the operator key must be kept safe outside the router.

### Confirmation

Agents refuse manifests that do not verify against `update.public_key`; `deploy.py --agent` refuses an unsigned binary
once a signing thumbprint is configured.
