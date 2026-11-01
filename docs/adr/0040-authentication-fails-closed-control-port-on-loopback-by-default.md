---
status: accepted
date: 2026-11-01
decision-makers: operator
---

# Authentication fails closed; the control port listens on loopback by default

> Source: `docs/security-review-2026-11-01.md` (SK-SEC-01), PR #70.

## Context and Problem Statement

The security review found two open doors in edge cases: with `client_auth.mode: enforce` and an empty client list, the
inference port let every request through; with no control users configured, the admin port granted access. The admin port
also listened on all interfaces by default, although it carries the web UI, the admin API and the agents' tunnel.

## Decision Drivers

* A missing or empty configuration must not mean "open".
* A fresh installation should not expose the admin API before an operator has configured users.
* Existing installations that configure their listeners explicitly must keep working.

## Considered Options

* Fail closed: `enforce` with no clients refuses inference; a control port without users serves only when it is bound to
  loopback (developer use); the default `control_listen` becomes `127.0.0.1:11435`
* Refuse to start with such a configuration
* Keep the permissive behaviour and document it

## Decision Outcome

Chosen option: "Fail closed with a loopback default", because it closes both doors without breaking development setups,
and an operator who wants remote agents and a remote UI says so with an explicit `control_listen`. Refusing to start was
not chosen: a router without clients is a valid intermediate state while setting up.

### Consequences

* Good, because an incomplete configuration no longer exposes inference or administration.
* Bad, because a new installation must set `control_listen` (for example `0.0.0.0:11435`) before remote agents can
  connect; the example configuration does.

### Confirmation

`test/selftest_security.py` checks the refused inference with an empty client list and the refused control access without
users on a non-loopback listener.
