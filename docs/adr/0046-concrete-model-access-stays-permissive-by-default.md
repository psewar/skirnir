---
status: superseded by ADR-0052
date: 2026-11-01
decision-makers: operator
---

# Access to concrete model names stays allowed by default for now

> Source: `docs/security-review-2026-11-01.md` (SK-SEC-05), PR #70.

## Context and Problem Statement

A client that is allowed certain roles can, by default, also request concrete model names, which bypasses the role
grant. The review proposed denying concrete names by default; in the same PR that default broke the existing memory and
session integrations, which call concrete models.

## Decision Drivers

* Role grants should mean what they say.
* Existing integrations must not break without a migration.

## Considered Options

* Keep the permissive default; operators set `models: false` for role-only clients; a stricter default only with a staged
  migration
* Deny concrete names by default now
* Remove concrete-name routing altogether

## Decision Outcome

Chosen option: "Keep the permissive default for now", because a default that breaks working integrations needs a
migration path first. The finding stays open in the review until a staged change (for example a warning phase, then a new
default) is decided in its own ADR.

### Consequences

* Good, because nothing breaks with this release.
* Bad, because a client without `models: false` can still bypass its role grant with a concrete model name.

### Confirmation

The review lists SK-SEC-05 as open; client configuration supports `models: false`.
