---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# The agent refuses signed downgrades

> Source: TM-13 in the threat model.

## Context and Problem Statement

The agent installs an update only with a manifest signed by the operator key and checks file, platform and SHA-256.
It refused the version it already runs, but any older manifest that was ever validly signed stayed installable. Whoever
controls the router, or replays an old order, could roll an agent back to a release with a known vulnerability.

## Decision Drivers

* A signature proves origin, not that a release is still safe to run.
* A deliberate recovery to an older release must remain possible for the operator.
* The router must learn the outcome from a fixed field, not from message text.

## Considered Options

* Refuse a signed manifest whose version is lower than the running release; `update.allow_downgrade: true` as the explicit
  recovery switch in the agent's own configuration
* Keep accepting any signed version (status quo)
* Put a minimum version into the signed manifest
* Let the router decide

## Decision Outcome

Chosen option: "Refuse older versions, explicit local recovery switch", because the agent can enforce it alone with what
it already knows (its own version and the signed manifest version), and the switch sits on the node, out of the router's
reach. A minimum version in the manifest would protect only against manifests signed after the change; deciding in the
router would trust exactly the component the check guards against.

* The comparison uses the signed manifest version after the signature check, never the unsigned order field alone.
* Release versions are `MAJOR.MINOR.PATCH` with digits only. While a release runs, a non-release target version is
  refused too. A local non-release build (`dev`) has no order to protect and is not checked.
* A refusal reports update kind `downgrade`.

### Consequences

* Good, because an old but validly signed manifest can no longer roll a release back.
* Bad, because rolling back on purpose needs the switch in the node's configuration (and a service restart) first.
* Neutral, because signing-key rotation and recovery stay open under TM-13.

### Confirmation

`agent/updater_test.go`: `TestDowngradeRefusal` (newer, equal-major, older, non-release and pre-release targets, the
switch, a dev build) and `TestUpdaterVerifyOrderRefusesSignedDowngrade` (a correctly signed older manifest is refused, the
switch allows it, a newer one passes).
