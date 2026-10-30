---
status: accepted
date: 2026-10-23
decision-makers: operator
---

# A standby Ollama serves while the OpenAI-compatible server is held

> Recorded retroactively on 2026-10-30; decided on 2026-10-23 (v0.19.0: router 0.12.0, agent 0.23.0); switching to the
> standby for a request added the same day (v0.22.0: router 0.15.0, agent 0.25.0). Source: CHANGELOG, README "Standby
> Ollama during a game", "Switching to the standby for a request".

## Context and Problem Statement

While the server (ADR-0032) is held for a game, the node has nothing to serve, not even the small `busy_ok` model of the
voice assistant. And while the server holds the card, models that only Ollama has cannot run on the node at all.

## Decision Drivers

* A node should keep serving small models during a game.
* A request for an Ollama model should be able to use the node even while the server holds the card.
* The server and Ollama must never share the card.

## Considered Options

* An Ollama child as standby (`tunnel.upstream_fallback`): the agent starts it once the server has stopped and stops it
  (after letting its answers finish) before the server starts again; the router treats the node as an Ollama node
  meanwhile; a request for a standby model may hold the server (a cold candidate with a score penalty)
* Leave the node without service while held
* Run Ollama and the server side by side

## Decision Outcome

Chosen option: "A standby Ollama with hold and release in order", because it gives the node something to serve in every
state and keeps the card for one of them at a time. The agent reports `serving: "ollama"` and the standby's models (also
while stopped, `standby_models`), so the router knows what a switch would offer.

### Consequences

* Good, because voice and small models keep working during a game and while the server is held.
* Bad, because each switch costs about a minute and stops one side; ADR-0036 makes switching wait for a gap.

### Confirmation

Selftests `selftest_openai_node_standby.py`, `selftest_standby_switch.py`; agent tests `upstream_fallback_test.go`,
`standby_test.go`.
