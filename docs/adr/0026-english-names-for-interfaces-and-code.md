---
status: accepted
date: 2026-10-22
decision-makers: operator
---

# Every interface and the code use English names

> Backfilled on 2026-10-30, decided before 2026-10-22, carried out on 2026-10-22 (v0.16.0 interfaces, v0.16.1
> code identifiers and messages, comments afterwards). Source: `design/english-names.md`.

## Context and Problem Statement

The project grew with German names: Home Assistant entities, journal events, GPU guard states, file names, identifiers,
log texts. The repository is public and meant to be usable by others.

## Decision Drivers

* Names that leave the process are an interface; changing them breaks automations, so do it once and completely.
* Mixed fleets (old and new agents) must keep working during the rollout.

## Considered Options

* English for everything that leaves the process, then code identifiers and messages, then comments; German stays where it
  is deliberate (German translations `*.de.md`, the German summary in release notes)
* Keep German names
* Rename gradually per module

## Decision Outcome

Chosen option: "English in three steps, interfaces first", because the interface step needs a migration and is done once.
The router reads both names from agents during the transition and translates old journal entries on load; Home
Assistant automations have to follow the new entity ids (no further compatibility layer).

### Consequences

* Good, because the project reads consistently for outside users.
* Bad, because existing automations had to be migrated; recorder history of renamed entities does not carry over.

### Confirmation

`design/english-names.md` holds the name tables; new code and interfaces are English (review).
