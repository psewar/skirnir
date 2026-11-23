---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# Game detection is a module that can be switched off

> Part of opening Skirnir to other setups (portability, phase 0). Applies
> [ADR-0026](0026-english-names-for-interfaces-and-code.md) (English names on every interface) to the game fact and the
> Home Assistant entity names that still carried German.

## Context and Problem Statement

Agent 0.17.0 added game detection: on Windows the agent scans the processes every second against the install folders of
known game publishers and reports `game` with every heartbeat, so the router marks a node busy while a game needs the
GPU. It was always on and had no configuration key. On Linux it reports `supported: false`. On a Windows machine that
never plays (a server, a workstation in an office) the scan is wasted work, and its process list and registry reads are
something an operator may not want at all.

Two German values remained on the wire and in Home Assistant: the game fact's `via` says `pfad` for a game found by its
path, and the Home Assistant entity names (`GPU-Schutz`, `STT-Dienst`, ...) are German, while ADR-0026 made interfaces
English.

## Decision Drivers

* An existing configuration behaves as before (detection on under Windows); a new key, not a changed default.
* The router knows "switched off" as a fact, distinct from "this platform cannot detect games".
* Renaming a wire value must not break a router or agent of the previous version.
* Home Assistant history and automations hang on `unique_id` and entity ID; those must not change. Dashboards show the
  display name, which existing users chose to see in German.

## Considered Options

* **`game.enabled` (default: on where the platform detects games), fact `enabled`; `via` becomes `path` with the router
  accepting both; `mqtt.entity_language: de | en` with German as default.**
* Switch game detection off by default everywhere. Rejected: it changes behaviour for existing Windows nodes, where the
  router relies on it to free the GPU for a game.
* Rename the Home Assistant names to English unconditionally. Rejected: it relabels every dashboard of existing users at
  the next agent update, for no functional gain; the entity IDs are already English (agent 0.20.0).

## Decision Outcome

Chosen option.

* `game.enabled` in the agent configuration. Unset means on where the platform detects games (Windows) and off
  elsewhere, which is what each platform did before. Switched off, the watcher does not start and the fact says
  `enabled: false` (`supported` keeps saying what the platform can do). The router treats `enabled: false` like a
  platform without detection: no game is known, a game seen before is forgotten.
* The game fact's `via` is `path` or `name`. The router maps `pfad` from agents before 0.36.0 to `path`, so its state,
  journal and Home Assistant only ever show the English value. The mapping can go once no older agent reports.
* `mqtt.entity_language` chooses the display names of the Home Assistant device: `de` (default, the names before agent
  0.36.0) or `en`. `unique_id` and the entity ID (`default_entity_id`) stay as they are in both languages. The setting is
  node-local; the router does not provision it.

### Consequences

* Good: an operator can turn off a scan that does nothing useful on a machine, and the router sees why there is no game
  fact.
* Good: no German value on the wire from agents 0.36.0 on; older agents keep working with a new router and vice versa
  (an older router stores `path` as it is).
* Good: English Home Assistant names are one line of configuration, without losing history.
* Bad: two languages of display names to keep in step; `agent/nogpu_test.go` checks that every entity has an English
  name.
* Neutral: on Linux `game.enabled: true` changes nothing, the platform still reports `supported: false`.

### Confirmation

`agent/nogpu_test.go` (default per platform, `enabled: false` stops the watcher, every entity has an English name,
English discovery keeps `unique_id` and entity ID), `agent/game_test.go` (detection by path reports `path`) and
`test/selftest_cpu_node.py` (router maps `pfad` to `path`, forgets the game when detection is switched off).
