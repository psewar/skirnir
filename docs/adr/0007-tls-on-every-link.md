---
status: accepted
date: 2026-09-07
decision-makers: operator
---

# TLS on every link; Ollama listens on localhost only

> Backfilled on 2026-10-30, decided on 2026-09-07. Source: README "Security model, in short".

## Context and Problem Statement

Ollama on a GPU node answered on the whole LAN without authentication, and the router's inference port spoke plain HTTP.
Prompts can contain personal data from the home.

## Decision Drivers

* No plain-text prompts on the network.
* No GPU node reachable by anyone on the LAN.
* No certificates to manage on the nodes.

## Considered Options

* TLS on every link (inference and admin port with the router's certificate, tunnel over WSS, MQTT over TLS), Ollama on
  localhost
* TLS only at the router's public edge
* Plain HTTP inside the home network

## Decision Outcome

Chosen option: "TLS on every link", because the home network is not a trust boundary. Nodes need no certificate of their
own: the agent reaches Ollama on localhost and talks to the router through its outbound tunnel (ADR-0008).

### Consequences

* Good, because nothing on the way sees prompts or answers in clear text.
* Good, because the open firewall rules for Ollama on the nodes could be removed.
* Bad, because clients have to trust the router's certificate.

### Confirmation

`api_tls: true` in the production configuration (the option itself defaults to off for development); with `api_tls`
the router refuses to start without a certificate (`app.py`). Nodes have no inbound firewall rule for Ollama.
