---
status: accepted
date: 2026-09-16
decision-makers: operator
---

# Settings that shift the trust base change only in the configuration file

> Recorded retroactively on 2026-10-30; decided with the build-out of UI settings on 2026-09-10 and tightened after a
> security review on 2026-09-16 (`client_auth.locked`). Source: README "Security model, in short", `settings.py` allowlist.

## Context and Problem Statement

Almost all runtime settings became editable in the web UI (scheduler weights, budgets, client data classes, roles). Some
settings decide who may do what: ports, TLS, identities, provider endpoints, egress, the client authentication mode. A
review found that the authentication mode could still be switched at runtime.

## Decision Drivers

* A stolen admin password must not be enough to open the inference port or redirect traffic.
* Everyday tuning should not need a deploy.

## Considered Options

* An allowlist of UI-editable settings (`settings.py`); everything that shifts the trust base only via the configuration
  file, and `client_auth.mode` frozen with `client_auth.locked`
* Everything editable in the UI
* Nothing editable in the UI

## Decision Outcome

Chosen option: "An allowlist of UI-editable settings", because it separates tuning from trust. With `locked`, switching the
authentication mode via UI or API answers 403 and is audited. Secrets live in `secrets.env` (0600), never in logs or
`/admin/state`; the router has no write access to the secret store.

### Consequences

* Good, because the admin port cannot widen access on its own.
* Bad, because changing a trust-relevant setting needs a deploy.

### Confirmation

`settings.py` holds the allowlist. `test/run_tests.py` checks that a path outside it (for example a provider's
`base_url`) is refused with 400 and that `locked` keeps the mode; `test/selftest_config_load.py` checks the refusal of the
locked mode in `settings.coerce`.
