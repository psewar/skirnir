---
status: accepted
date: 2026-09-07
decision-makers: operator
---

# The agent keeps an outbound tunnel; the router reaches Ollama only through it

> Recorded retroactively on 2026-10-30; decided on 2026-09-07 (agent 0.3.0), legacy token tunnel removed on 2026-09-25
> (v0.5.1). Source: README "Architecture", "Interfaces", CHANGELOG v0.5.1.

## Context and Problem Statement

The router has to call Ollama on each node (models, loading, inference). Calling the nodes directly needs an open port,
a certificate and a fixed address per node; a TLS proxy in front of Ollama with a pinned fingerprint (agent 0.2.0) worked,
but every node still needed a firewall rule.

## Decision Drivers

* No open port, no certificate and no fixed IP on a node.
* A node that goes away is noticed at once.
* Streaming and cancellation end to end.

## Considered Options

* An outbound WebSocket from the agent to the router, multiplexing all Ollama calls (REQ/RESP/DATA/END/ERR/CANCEL)
* The router calls each node's Ollama directly over a TLS proxy (pinned fingerprint)
* mTLS between router and nodes

## Decision Outcome

Chosen option: "An outbound WebSocket from the agent", because the node only dials out: nothing to open, nothing to
certify, and a dropped tunnel marks the node offline immediately. mTLS would be redundant to the key-based login of the
tunnel (ADR-0009). Nodes without an agent can still be configured with a direct URL.

### Consequences

* Good, because a new GPU machine needs the agent and a router URL, nothing else.
* Good, because a client disconnect becomes a CANCEL down to Ollama.
* Bad, because all node traffic depends on the tunnel; a router restart costs the nodes 1–2 s.

### Confirmation

E2E tests serve nodes through a fake agent over the tunnel; the router's node calls (`nreq`) prefer the tunnel.
