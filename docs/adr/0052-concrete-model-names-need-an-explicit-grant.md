---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# Concrete model names need an explicit grant

> Supersedes [ADR-0046](0046-concrete-model-access-stays-permissive-by-default.md). Source: SK-SEC-05 in
> `docs/security-review-2026-11-01.md`, TM-04 in the threat model.

## Context and Problem Statement

A client restricted to certain roles could still request a concrete model name, because a missing `models` setting meant
`true`. ADR-0046 kept that default until a migration path existed: the strict default had broken the memory and session
integrations, whose tests and deployments call concrete models without saying so.

## Decision Drivers

* A role grant should mean what it says (TM-04, P1 for clients with different rights).
* Integrations that need concrete names must keep working once they say so.
* An operator must see which clients change before traffic is refused.

## Considered Options

* Deny concrete names unless `models: true`, warn at load time for clients without the setting
* Keep the permissive default (ADR-0046)
* A warning-only release first, the new default in a later release
* Per-client model allowlists instead of a boolean

## Decision Outcome

Chosen option: "Deny concrete names unless `models: true`, warn at load time", because the migration is one line per client
and the load-time warning names every affected client before the first refused request. A warning-only release would
delay the fix without giving operators more information than the warning does now. Allowlists remain a possible later
refinement; they do not change the default.

* Only the boolean `true` grants concrete names; a non-boolean `models` value is a configuration error.
* A client without `models` gets `false` and a warning, unless it has `inference: false` (it cannot infer at all).
* The router's own UI client keeps concrete names (measurement and tests from the control port).
* `expose_concrete_models` still decides whether concrete names are routed at all.

### Consequences

* Good, because a role-only client can no longer bypass its role grant with a concrete model name.
* Good, because the integrations keep working with an explicit `models: true`, the same migration the repository's tests use.
* Bad, because a deployment that relied on the old default refuses concrete names after the upgrade until it adds
  `models: true` (403 with the reason "concrete model names not allowed (roles only)").
* Neutral, because model listings (`/api/tags`, `/v1/models`) still show concrete names to role-only clients; that is
  information, not access.

### Confirmation

`test/selftest_security.py` covers a missing setting, `models: true`, a non-boolean value, the UI client and the
configuration default; the memory, session and context self-tests run with explicit grants.
