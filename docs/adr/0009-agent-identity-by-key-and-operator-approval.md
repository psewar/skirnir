---
status: accepted
date: 2026-09-07
decision-makers: operator
---

# Agents identify with an Ed25519 key and need the operator's approval

> Backfilled on 2026-10-30, decided on 2026-09-07 (agent 0.4.0); shared heartbeat token removed. Source:
> README "GPU nodes: the agent", "Security model".

## Context and Problem Statement

The first tunnel authenticated agents with a shared token from the router's configuration, and each node needed secrets
of its own (for example MQTT access) copied onto it.

## Decision Drivers

* A stolen token must not let anyone attach a node.
* No secrets to distribute to nodes by hand.
* The operator decides which machines may serve.

## Considered Options

* Per-agent Ed25519 key (generated on first start, DPAPI-protected on Windows), challenge login, approval in the router
  UI, configuration package after approval
* Shared token per installation
* Client certificates (mTLS)

## Decision Outcome

Chosen option: "Per-agent Ed25519 key with approval", because the key never leaves the node, unknown keys wait as
`pending` until approved, and approved agents receive what they need (heartbeat interval, optional MQTT access) through
the tunnel. Revoking a node is one click.

### Consequences

* Good, because nodes carry no shared secrets; the router registry (`nodes.json`) holds pending/approved/revoked keys.
* Good, because a new machine shows up for approval by itself (UI and a home automation sensor).
* Bad, because a reinstalled node gets a new key and needs approval again.

### Confirmation

The tunnel handshake requires a valid signature over the router's challenge; unapproved keys get no requests.
