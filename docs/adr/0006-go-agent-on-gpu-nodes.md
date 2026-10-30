---
status: accepted
date: 2026-09-07
decision-makers: operator
---

# A Go agent runs on every GPU node

> Recorded retroactively on 2026-10-30; decided by 2026-09-07 (agent 0.2.0/0.3.0), replacing a PowerShell task from
> 2026-09-04. Source: `design/routing-algorithm.md` section 6 ("entschieden: Go-Agent … der PowerShell-Task ist
> entfernt"), `agent/README.md`.

## Context and Problem Statement

The router needs GPU load and VRAM from every node every few seconds, and later more: Ollama as a supervised child,
sensors, a tunnel, updates. The first node had no Python. The first version was a PowerShell scheduled task that called
`nvidia-smi` and posted a heartbeat.

## Decision Drivers

* Runs on Windows and Linux without a runtime installed on the node.
* Reliable heartbeat and direct GPU access (NVML) instead of parsing tool output.
* Room to grow: tunnel, child processes, updates, a Windows service.

## Considered Options

* A Go single binary (NVML, Windows service / Linux binary)
* PowerShell scheduled task with `nvidia-smi`
* A Python agent

## Decision Outcome

Chosen option: "A Go single binary", because it needs nothing installed on the node, reads NVML directly, and runs as a
proper Windows service. The design note had recommended PowerShell first "if the heartbeat proves reliable"; the agent's
duties (tunnel, children, identity) outgrew a script within days.

### Consequences

* Good, because a new node needs one binary and one configuration file.
* Good, because the agent can supervise Ollama and other services as children (ADR-0021).
* Bad, because there are two languages in the repository (Python router, Go agent).

### Confirmation

`agent/` builds for `windows/amd64` and `linux/amd64` in CI; there is no PowerShell agent left.
