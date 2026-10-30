---
status: accepted
date: 2026-10-24
decision-makers: operator
---

# The agent starts the server held and checks RAM before every start

> Recorded retroactively on 2026-10-30; decided on 2026-10-24 (v0.24.0: router 0.17.0, agent 0.26.0; v0.24.1: agent
> 0.26.1). Source: CHANGELOG, README "Start held, RAM check before a start", PR #66, #67.

## Context and Problem Statement

After every restart the agent started the OpenAI-compatible server (about 38 GB of experts into RAM) although the router
keeps it held outside its window and held it again within seconds. One such start at 98 % RAM was followed by a
bluescreen. The router cannot judge the server's RAM need (ADR-0035); the agent can measure it.

## Decision Drivers

* A restart must not load the server only for the router to hold it.
* No start of the server when RAM would exceed a limit.
* The agent decides from what it measures and reports it as a fact (ADR-0022).

## Considered Options

* The agent remembers the server's models and RAM need (peak private bytes of its process tree in the last run) in
  `<identity dir>/upstream.json`; with a standby and that file it starts with the server held; before every start it
  checks RAM in use plus that need against `tunnel.upstream_fallback.max_ram_percent` (default 90) and otherwise stays held
  and reports `upstream_blocked`
* The router decides at startup whether the server may run
* Remove the server child from the agent configuration by hand

## Decision Outcome

Chosen option: "Start held with remembered models, RAM check in the agent", because the agent has the measurement and the
process, and the router keeps deciding *when* the server should run. Without the file (first start) the server starts once
and the agent learns both; since v0.24.1 the hold itself takes the models too, because outside its window the router holds
the server seconds after it answers, before any periodic sample. The router offers no release candidate while
`upstream_blocked` stands and answers `memory_pressure` with the numbers.

### Consequences

* Good, because a restart no longer loads the server, and no start happens without room in RAM.
* Bad, because with a need of about 41 GiB on a 64 GiB machine the server starts only when little else runs; in normal use
  a request for it is refused with `memory_pressure`.

### Confirmation

Agent tests `upstream_start_held_test.go` (held start, refused release, learning without a sample) and
`procmem_windows_test.go`; router selftest `selftest_upstream_blocked.py`.
