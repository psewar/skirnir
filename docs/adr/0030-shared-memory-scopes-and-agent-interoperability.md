---
status: accepted
date: 2026-10-19
decision-makers: operator
---

# Shared memory scopes and agent handoff are off by default and keep private banks unchanged

> Backfilled on 2026-10-30, decided on 2026-10-19 (v0.14.0), shared scopes readable next to a private bank
> since 2026-10-21 (v0.15.2), scope curation since 2026-10-22 (v0.15.6). Source:
> `design/shared-memory-interoperability.md`, `design/memory-reliability.md`.

## Context and Problem Statement

Several agents and automations should share knowledge (for example facts the operator or a coding agent writes for an
assistant) and hand tasks to each other, without mixing private memories or exposing them to clients that only identify
by IP.

## Decision Drivers

* Existing private banks must keep their identity and behaviour.
* Shared access is explicit per client and scope; IP-only identity never grants memory or session access.
* Secrets and namespace keys stay out of the YAML configuration.

## Considered Options

* Shared scopes and signed handoff packages as additional, default-off features; bank identities derived by HMAC from a
  stable namespace key (`v1`, principal, project, classification); existing clients keep `strategy: legacy`; recall from a
  shared scope next to the private bank via `recall_with`
* One shared bank for everyone
* No sharing; each agent keeps its own memory

## Decision Outcome

Chosen option: "Default-off shared scopes with HMAC bank identities", because nothing changes for existing clients and
every shared read or write is a grant. Durable admission, recovery and deletion are part of the design
(`design/memory-reliability.md`); the Hindsight SDK is a test dependency, not a runtime one.

### Consequences

* Good, because private and shared knowledge stay separated by construction.
* Bad, because changing the namespace key changes bank identities; it must be kept stable.

### Confirmation

Selftests for scopes, HMAC identities and handoff signatures (`selftest_interoperability`, `selftest_reliability`);
integration test `integration_interoperability`.
