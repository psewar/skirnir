---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# New agents enroll only from configured networks

> Threat: TM-07 (next action "limit enrollment to known networks"). Complements
> [ADR-0066](0066-explicit-agent-key-creation-and-lost-key-recovery.md) (explicit agent keys; `skirnir-agent identity`
> for the out-of-band fingerprint check).

## Context and Problem Statement

Any host that reaches the router's tunnel endpoint can create an Ed25519 key and enroll: it appears as *pending* in the
web UI and waits for approval. The router bounds the damage (32 pending at most, 6 admissions per address in 300 s),
and the operator compares fingerprints before approving (`skirnir-agent identity`, ADR-0066). Still, a host outside the operator's networks (an
exposed router, a guest network, a VPN peer) can fill the pending slots or show up under a convincing name and hope for
a hurried approval. Most installations know exactly which networks their GPU nodes live in.

## Decision Drivers

* Unknown keys from outside the operator's networks should never reach the registry.
* Known agents keep working when their address changes (DHCP, VPN, roaming laptop).
* An existing configuration keeps working unchanged.
* The check runs before anything is stored or any slot is used.

## Considered Options

* **`router.agent_enrollment_networks`: a CIDR list; a key not yet in the registry is refused from addresses outside
  it; known keys connect from anywhere.**
* Restrict all agent connections (also approved ones) to the networks. Rejected: an approved key is already trusted by
  the operator; refusing it after an address change would break nodes without adding protection against the threat
  (unknown keys).
* Enrollment invitations or one-time tokens. Not now: a larger change to agent installation; the network list covers
  the common homelab case and does not exclude invitations later.

## Decision Outcome

Chosen option: "a CIDR list for new keys", because it keeps unknown hosts from outside out of the registry with one
configuration line and no change to the agent.

* `router.agent_enrollment_networks` (list of IPv4/IPv6 networks; host bits may be set, a single address is a /32 or
  /128). Absent or empty = any address, as before. The configuration refuses anything else.
* `registry.enrollment_denial` checks it right after recognising a known key (which passes as before) and before the
  pending capacity and the per-address admission count; a refused key uses no admission slot. The agent gets the status
  message with the reason and the connection closes with 1008, as for the other enrollment refusals.
* An IPv4-mapped IPv6 peer address counts as its IPv4 address; an address the router cannot read is outside.

### Consequences

* Good, because a host outside the operator's networks can no longer enroll or fill pending slots.
* Good, because approved agents are unaffected by address changes.
* Bad, because behind a reverse proxy the router sees the proxy's address; the list then has to name the proxy, and
  the protection moves to the proxy.
* Neutral: the 1008 refusal and its message are the same path as the existing enrollment limits.

### Confirmation

`test/selftest_enrollment_networks.py` (in CI): networks parse (CIDR, single address, host bits) and bad values are
refused; addresses inside, outside, IPv4-mapped, unreadable and missing are matched as described; without networks any
address enrolls; a new key from outside is refused with the reason and uses no admission slot (rejected case), from
inside (IPv4 and IPv6) it enrolls; approved and pending keys reconnect from outside; the configuration loads the list
and refuses a string, a non-network and a number.
