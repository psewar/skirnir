---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Status sinks instead of a fixed Home Assistant publisher

> Part of the portability roadmap, phase 1 ([design/portability.md](../../design/portability.md)). Builds on
> [ADR-0013](0013-observability-without-opentelemetry.md) (observability without OpenTelemetry) and
> [ADR-0022](0022-components-report-state-as-facts.md) (components report state as facts), and on
> [ADR-0101](0101-problems-are-structured-records-exported-as-metrics.md) (problems are structured records exported as
> metrics). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

The router pushes its state to the operator through one fixed publisher: `HAPublisher` in
`router/skirnir_router/ha.py` (line 123), started in `router/skirnir_router/app.py` (around line 187) when `mqtt.host`
is set. It publishes a snapshot (`ha_snapshot`, line 40) as Home Assistant MQTT discovery entities under the prefix
`discovery_prefix` (default `homeassistant`, line 137) and the base topic `skirnir-router`. The queue between the core
and the publisher is named after it: `state.MQTT_DIRTY` and `state.MQTT_EVENTS` (`router/skirnir_router/state.py`,
lines 18-20).

Since ADR-0101 the problems (no node online, a role not servable, a lost heartbeat, a model spilled to RAM, GPU
protection, failed agent or Ollama updates, cloud budget) are structured records computed in
`router/skirnir_router/problems.py`, independent of MQTT. They can be pulled from `/metrics` (`skirnir_problem`),
`GET /admin/problems` and the web UI, and `ha_snapshot` takes their texts (`ha.py`, around line 95). Pushed to the
operator, though, they are still only through the Home Assistant publisher: people who use ioBroker, openHAB, Node-RED,
ntfy, Gotify or Matrix and run no Prometheus get no notification, and they see discovery entities they did not ask for
when they read the raw topics.

Agents publish their own MQTT device. The router hands each approved agent an MQTT login in the provisioning bundle:
`provision_bundle` in `router/skirnir_router/registry.py` (lines 142-154) copies `mqtt.username` and the router's
password (`read_mqtt_password`, line 135), so every node holds the router's broker credential. The agent's discovery
prefix is fixed to `homeassistant` (`agent/config.go`, line 430) and the router does not pass its own
`discovery_prefix` on.

## Decision Drivers

* An operator without Home Assistant must learn about the same problems.
* Several destinations at once (Home Assistant for the household, a webhook for the person on call).
* Existing configurations keep working unchanged: the current behaviour is the default.
* A node should not need the router's broker credential.
* Report state as facts; no destination parses another destination's text.

## Considered Options

* A `StatusSink` interface with several implementations, any number active at once.
* Keep Home Assistant only and document the raw topics for other systems.
* Prometheus/Alertmanager as the only generic path (structured problems in `/metrics`, phase 0), nothing else.
* A generic outbound webhook only, Home Assistant through it.

## Decision Outcome

Chosen option: "A `StatusSink` interface with several implementations", because it serves Home Assistant users exactly as
today and gives everyone else a direct path without a second monitoring stack.

* Interface: `start()`, `publish_state(snapshot)`, `publish_event(event)`, `sync_nodes(names)`, `stop()`. The snapshot
  and the events carry the structured problem records of ADR-0101 (`code`, `severity`, `node`, `subject`, `params`,
  `message`), not only display strings.
* Implementations in the repository: `mqtt_ha` (today's publisher, discovery and raw topics), `mqtt_plain` (raw state
  and event topics without discovery, for ioBroker, openHAB, Node-RED), `webhook` (HTTP POST of events and problem
  changes, for ntfy, Gotify, Matrix bridges, n8n; target URL and secret from the configuration and environment, egress
  explicit), `none`.
* Configuration: a list of sinks, several at once. Without the new key, an existing `mqtt:` section means one `mqtt_ha`
  sink with today's settings, so nothing changes.
* `state.MQTT_DIRTY` / `state.MQTT_EVENTS` become sink-neutral (`STATUS_DIRTY`, `STATUS_EVENTS`); every sink reads the
  same snapshot.
* Agents: the router passes its discovery prefix in the bundle, and the bundle can carry a per-node MQTT login (or none,
  with the agent's MQTT device off) instead of the router's own credential.

### Consequences

* Good, because users of other home automation systems and of push services get problems directly.
* Good, because the router's broker credential no longer has to be on every node once per-node logins are used.
* Good, because the problem list is produced once and rendered per destination.
* Bad, because a webhook sink is a new outbound destination: it needs an egress decision, a timeout and a bound on retries
  so a slow receiver cannot hold the router (B9, TM-08).
* Bad, because per-node MQTT logins need broker-side accounts, which the operator has to create.
* Bad, because the migration touches `ha.py`, `state.py`, `registry.py` and the agent's MQTT code in one series of PRs.

### Confirmation

Self-tests per sink with a fake broker or HTTP receiver: the default configuration produces exactly today's topics and
payloads (the existing tests that use `ha_snapshot` and `HAPublisher`, such as `test/selftest_problems.py`, `test/selftest_english_names.py`,
`test/selftest_power.py` and `test/selftest_slowness_signals.py`, stay green unchanged), several sinks receive the same problems, a failing
webhook does not block the publish loop, and an unknown sink kind is refused at configuration load. A test checks that
the provisioning bundle contains the router's password only when no per-node login is configured.
