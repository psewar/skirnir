---
status: proposed
date: 2026-11-03
decision-makers: maintainer
---

# Bind a pre-PR security review to the changed tree and repeat it in CI

## Context and Problem Statement

A one-time threat model becomes stale as routes, grants, providers and operational paths change. Documentation alone
cannot detect a missing or stale review. Model-based analysis is useful but non-deterministic and must not become a
privileged execution path for attacker-controlled PR content.

## Decision Drivers

* Find new attack surfaces before opening a PR, with explicit rejected-case tests.
* Keep the runtime and existing inference configuration unchanged.
* Make missing/stale evidence and expired risk acceptances deterministic failures.
* Allow local models and avoid automatic source disclosure to an unselected provider.

## Considered Options

* Documentation-only advice
* A model verdict as a mandatory build gate
* Change-bound pre-PR attestation plus deterministic checks and opt-in advisory AI

## Decision Outcome

Proposed option: change-bound attestation plus checks and advisory AI. AGENTS.md requires the repository skill before
every PR/update. A fixed JSON record binds to merge-base and the whole candidate tree (paths, modes and blob IDs),
excluding only that non-executable record to avoid a self-hash cycle. Missing/stale records fail the PR check.
Baseline risks remain open, not retroactively accepted. Tests and risk references state their incomplete coverage.

The default-branch workflow rechecks candidate Git objects after CI through workflow_run; it never checks out or
executes PR code, downloads PR artifacts, installs PR dependencies or reads PR instructions as authority. It has
contents:read, no write credentials. Model use is opt-in, requires an operator-chosen endpoint/model and can use a
local endpoint in manual execution. Source is data, no model tools are offered, output is an untrusted JSON artifact.
Weekly and manual runs inspect the full tracked source in bounded batches, explicitly reporting omitted scope.

### Consequences

* New commits invalidate evidence; documentation and Dependabot PRs also need an honest review record.
* The record proves freshness and shape, not that a reviewer was truthful or that the code is safe.
* No API/router/agent version bump: development tooling only. Maintainer must configure required checks/rulesets.
* AI is skipped until explicitly configured, does not veto/approve merges, and cannot replace tests or human review.
* Changes to this mechanism need explicit maintainer scrutiny. Branch rules/CODEOWNERS remain external enforcement;
  a malicious contributor can edit a PR workflow, while the trusted recheck detects record/policy inconsistencies.
* Workflow-run review starts after merge of this infrastructure; the introducing PR is reviewed locally and in its PR CI.

### Confirmation

`test/selftest_threat_review.py` covers stale evidence, deletions/modes, malformed inputs, expiry, endpoint transport,
redirect refusal, output validation, batch coverage and the non-executing advisory boundary. Existing security tests
gain wrong-token/IP, concrete-model, cloud opt-out and pending-cap negative cases. Run the skill on this PR itself.
