---
status: accepted
date: 2026-11-01
decision-makers: operator
---

# A data class configured for a client is a floor its requests cannot lower

> Source: `docs/security-review-2026-11-01.md` (SK-SEC-03), PR #70. Refines ADR-0014 without superseding it.

## Context and Problem Statement

Under ADR-0014 a client declares the data class of a request (`routing.data_class`), and the operator can configure a
class per client (`clients.<c>.data_class`). The review found that a request could declare a lower class than the one the
operator configured for its client, and so reach a cloud tier the operator meant to close.

## Decision Drivers

* A class the operator configured for a client must hold for all its requests.
* Clients without a configured class keep declaring their own class, as ADR-0014 intends.
* A request may always raise its class.

## Considered Options

* Floor only where configured: with `clients.<c>.data_class` set, the effective class is the higher of it and the
  request's; without it, the request's declaration applies, and undeclared requests get `default_data_class`
* Floor always: the configured class or, without one, `default_data_class` (`personal`) is the floor, so a client without
  a configured class can never declare a lower class
* No floor (previous behaviour)

## Decision Outcome

Chosen option: "Floor only where configured", decided by the operator on 2026-11-01. It closes the gap the review found
and keeps the semantics of ADR-0014: the configured class is the server-side floor, the request's declaration is the
client's statement. Making `default_data_class` a floor for everyone was rejected because clients that declare `internal`
per request would silently lose their cloud tier.

### Consequences

* Good, because an operator can pin a client to `personal` and no request of it reaches the cloud by declaring less.
* Bad, because an unconfigured client can still declare `internal`; the operator has to configure a class for clients
  that must not.

### Confirmation

`test/selftest_security.py` checks that a configured floor holds and can be raised, that an unconfigured client may
declare `internal`, and that an undeclared request stays at the default.
