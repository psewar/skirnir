---
status: superseded by ADR-0086
date: 2026-11-01
decision-makers: operator
---

# The systemd units are hardened; the services still run as root for now

> Source: `docs/security-review-2026-11-01.md` (SK-SEC-07), PR #70.

## Context and Problem Statement

The router and the Linux agent run as root. The router writes its configuration directory, registry and journal; the
agent updates its own binary. A dedicated user needs ownership changes on the hosts that have not been tested.

## Decision Drivers

* Reduce what a compromised process can do now, without an untested change of user.
* Keep the router's writes and the agent's self-update working.

## Considered Options

* Harden the units now (empty capability bounding set for the router, no new privileges, no SUID/SGID, protected kernel
  tunables, modules and control groups, locked personality, native system calls only) and keep root until an unprivileged
  deployment is tested
* Switch to a dedicated user now
* Leave the units as they are

## Decision Outcome

Chosen option: "Harden now, keep root for now", because it removes capabilities and kernel interfaces the services never
use, with little risk for existing deployments. Running unprivileged stays open until it has been tested on a real
deployment.

### Consequences

* Good, because a compromised router has no capabilities left, even as root.
* Bad, because some hardening options need namespace support; on container hosts the first deploy of the new unit has to
  be watched, with the old unit kept for rollback.

### Confirmation

The unit files carry the options; a deploy checks that the service is active after the restart.
