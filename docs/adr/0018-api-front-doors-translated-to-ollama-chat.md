---
status: accepted
date: 2026-10-01
decision-makers: operator
---

# OpenAI and Anthropic APIs are translated to Ollama's chat inside the router

> Backfilled on 2026-10-30, decided before the first public release (2026-09-18) for the OpenAI-compatible `/v1`, and on
> 2026-10-01 for the Anthropic Messages API (v0.12.0). Source: README "Interfaces", "Anthropic clients",
> CHANGELOG v0.12.0.

## Context and Problem Statement

Clients speak different APIs: the Ollama API, the OpenAI API (many tools and agent frameworks) and the Anthropic Messages
API (coding agents). Ollama has its own `/v1`, but it knows neither `num_ctx` nor `keep_alive`.

## Decision Drivers

* Roles, context tiers and `keep_alive` must apply to every client, whatever API it speaks.
* One internal request path for scheduling, limits and metrics.

## Considered Options

* The router translates `/v1/*` and `/v1/messages` to Ollama's `/api/chat` itself (streaming, tools, images, structured
  output, thinking)
* Pass `/v1` through to Ollama's own OpenAI layer
* Separate services per API

## Decision Outcome

Chosen option: "The router translates to `/api/chat`", because only then do roles, context tiers and `keep_alive` apply to
OpenAI and Anthropic clients as well. The same canonical path also serves OpenAI-compatible node backends (ADR-0032) and
cloud tiers, whose answers come back as Ollama chunks.

### Consequences

* Good, because every client gets the same routing, admission and metrics.
* Bad, because the router has to follow API details of three protocols (for example system messages inside `messages`,
  thinking blocks); acceptance runs with real clients found such cases.

### Confirmation

E2E tests per API (streaming and not, tools, images); acceptance run with a real Anthropic client.
