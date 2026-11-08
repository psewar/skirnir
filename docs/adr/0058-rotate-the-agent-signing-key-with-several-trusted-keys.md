---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# Rotate the agent signing key with several trusted keys

> Source: TM-13 in the threat model. Complements ADR-0054 (no signed downgrades).

## Context and Problem Statement

An agent trusted exactly one operator key (`update.public_key`). Replacing it meant changing every node and the signing
key at the same moment; any node not yet changed would refuse the next release, and a node changed too early would refuse
the current one. Without a gap-free rotation, a leaked or aging signing key tends to stay in use.

## Decision Drivers

* Rotation must be possible without a window in which updates fail.
* Trust stays node-local: the router must not be able to add a trusted key.
* The operator steps must be tools that check their own preconditions.

## Considered Options

* `update.public_keys` (a list next to `public_key`); a manifest is valid if any configured key verifies it; two
  `deploy.py` steps prepare and switch the signing key
* A signed key-rollover message pushed by the router
* Keep one key and change it everywhere at once

## Decision Outcome

Chosen option: "A list of trusted keys plus two deploy steps", because it needs no new protocol and keeps every trust
decision in the node's own configuration. A key rollover through the router would let a compromised router or signing
key extend its own trust.

* Agent: `update.public_key` and `update.public_keys` together form the trusted set; every entry must be a valid
  Ed25519 key (a typo is reported with its position instead of shrinking the set); the signature must verify under one of
  them.
* Procedure: `deploy.py --new-agent-key` creates `agent-update.next.key` and prints its public part; the operator adds it
  to `update.public_keys` on every node; `deploy.py --use-next-agent-key` makes it the signing key and keeps the old one
  as `agent-update.retired-<time>.key`; once every node runs a release signed with the new key, the old public key is
  removed from the nodes. If the router's optional `modes.agent_update.public_key` check is used, it is switched with
  the signing key. A compromised key is revoked the same way, by removing it from the nodes.
* Both deploy steps refuse when their precondition is missing.

### Consequences

* Good, because the signing key can be replaced without a window of failing updates.
* Good, because a leaked key can be dropped node by node without waiting for a router change.
* Bad, because during a rotation two keys are trusted; the window ends only when the old key is removed from the nodes.
* Neutral, because the router's optional self-check still takes a single key.

### Confirmation

`agent/updater_test.go` `TestUpdaterAcceptsAnyConfiguredKeyForRotation` (old and new key accepted together, an unlisted
key refused, the old key refused once removed, `public_keys` alone, an invalid entry reported);
`test/selftest_key_rotation.py` for the two deploy steps.
