---
status: accepted
date: 2026-09-10
decision-makers: operator
---

# Configuration is checked against a schema before it goes live; canary and shadow per role

> Recorded retroactively on 2026-10-30; decided on 2026-09-10 (roadmap stage 6). Source: `design/roadmap.md`, README
> "Operations-ready".

## Context and Problem Statement

A typo in the configuration could stop the router after a deploy; a new model in a role could answer worse without anyone
noticing; a retried request could run twice.

## Decision Drivers

* A broken configuration must never replace a working one.
* New models should be tried on part of the traffic before they take over.
* Retries must not double expensive work.

## Considered Options

* A configuration schema with suggestions on typos (`router.py --check`), checked locally before the push and on the host
  before the restart (fail-closed); idempotency key for non-streaming requests; canary and shadow tiers per role
* Validation only at startup
* No canary/shadow; switch models directly

## Decision Outcome

Chosen option: "Schema check before activation plus canary and shadow", because the deploy refuses a configuration the
schema rejects and leaves the old process running. Canary sends a share of a role's requests to a new tier; shadow runs it
after the answer, never in parallel. Signatures and SBOMs were left out; a deploy manifest with SHA-256 and the Ollama
versions and model digests per node give the supply-chain view.

### Consequences

* Good, because a configuration error is caught before the restart.
* Bad, because a newly added configuration key needs a router version that knows it before the configuration may use it.

### Confirmation

`deploy.py` runs the schema check locally and on the host; the CI runs `router.py --check` on the example configuration.
