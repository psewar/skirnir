---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# The control port requires users even on loopback

> Source: TM-02 in the threat model. Refines the fail-closed control port of ADR-0041.

## Context and Problem Statement

Without `control_auth.users` the control port refused network requests but served loopback peers on a loopback listener
without any authentication. A reverse proxy on the same host also connects from loopback, so publishing the control port
through such a proxy exposed the UI and every `/admin/*` action without credentials, before the CSRF check.

## Decision Drivers

* Loopback says where a connection comes from, not who is behind it.
* Development and tests without a password must stay possible, but only on purpose.
* Exceptions are explicit and visible, never a silent default.

## Considered Options

* Answer 503 without users; unauthenticated loopback only with `control_auth.allow_unauthenticated_loopback: true`
* Keep the implicit loopback exception (status quo)
* Remove unauthenticated access completely
* Detect proxies from forwarding headers

## Decision Outcome

Chosen option: "503 without users, explicit development switch", because it closes the proxy path by default and keeps
password-free development as a named, logged choice. Forwarding headers are set by the proxy and cannot prove the absence
of one; removing the mode completely would force passwords into throwaway test setups for no gain.

* The switch is a boolean (anything else is a configuration error) and only works with a literal loopback listener
  (`127.0.0.1` or `::1`) and a loopback peer.
* The router logs a warning at start whenever no control users are configured, naming the switch when it is on.

### Consequences

* Good, because a misconfigured proxy in front of a user-less control port no longer exposes administration.
* Bad, because a local development setup without users now needs the switch (breaking for such setups only).

### Confirmation

`test/selftest_security.py` covers loopback without the switch (503), with the switch (allowed), the switch with a remote
peer and with a wildcard listener (503); `test/selftest_config_load.py` covers the default and a non-boolean value.
