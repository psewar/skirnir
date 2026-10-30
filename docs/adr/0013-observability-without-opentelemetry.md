---
status: accepted
date: 2026-09-10
decision-makers: operator
---

# Observability through a Prometheus endpoint, a journal and MQTT discovery; no OpenTelemetry

> Backfilled on 2026-10-30, decided on 2026-09-10 (roadmap stage 4). Source: `design/roadmap.md`, README
> "Observability", "Home Assistant".

## Context and Problem Statement

The operator needs to see why a request went where it went, what each client uses, and the state of every node, mostly in
the home automation system that is already there.

## Decision Drivers

* No extra infrastructure for a small installation.
* The home automation system is the operator's dashboard and alerting.
* Decisions must be explainable after the fact.

## Considered Options

* `GET /metrics` in Prometheus text format (no extra library), usage per day/client in a JSON file, a decision journal
  (`events.jsonl`), and a Home Assistant device via MQTT discovery
* OpenTelemetry traces and metrics
* Logs only

## Decision Outcome

Chosen option: "Prometheus endpoint, journal and MQTT discovery", because they need no collector and fit what exists.
OpenTelemetry was deliberately left out: there is no collector in the installation. The endpoint was built ready for a
Prometheus server; on 2026-09-25 a Prometheus (90 days retention) and Grafana pair was added next to the router
(`monitoring/`), because a router is not a time-series database. The Home Assistant part is optional; installations
without it lose nothing else.

### Consequences

* Good, because every routing decision and refusal is in the journal with its reason.
* Good, because problems (node missing, GPU guard, budget) appear as entities in the home automation system.
* Bad, because there is no distributed tracing across clients and nodes.

### Confirmation

`GET /metrics` and `/admin/usage` are covered by tests; MQTT stays off without broker configuration.
