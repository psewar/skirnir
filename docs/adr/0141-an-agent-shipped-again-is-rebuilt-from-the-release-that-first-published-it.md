---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# An agent shipped again is rebuilt from the release that first published it

> Extends [ADR-0075](0075-release-provenance-by-reproducible-rebuild.md) (release provenance by reproducible rebuild).
> Threat: TM-16 (next action "extend the rebuild to releases whose agent came from an earlier commit").

## Context and Problem Statement

A release ships the current agent binaries even when only the router changed: router 0.34.1 and agent 0.35.2 in v0.39.1,
the same agent files as in v0.36.0. *Release provenance* rebuilt them from the release's own tag. That works while
`agent/` stays as it was when the binaries were built. When it changed without a new agent (v0.35.1: a Dependabot bump
of `golang.org/x/sys` in `agent/go.mod`), the tag no longer reproduces the published Windows binary, the job failed with
both hashes, and the release got no attestation and no SBOMs, although its binaries came unchanged from v0.34.1.

## Decision Drivers

* Attest only bytes a rebuild reproduces; never weaken the comparison.
* A router release must not lose its agent attestation because `agent/` changed in between.
* Older releases can be attested again by hand.

## Considered Options

* **Rebuild from the tag; if that does not reproduce the binaries, rebuild from the release that first published this
  agent version, and attest the binaries when that reproduces them.**
* Record the agent's source commit in the release (a file or the notes) and rebuild from it. Rejected for now: it needs
  the maintainer's release steps to write it, and releases made so far do not carry it; the first release with the
  version is derivable from the published assets.
* Skip the agent for releases that ship an earlier agent. Rejected: the release would carry unattested binaries.

## Decision Outcome

Chosen option: "the tag, then the release that first published the agent version", because it derives the source from
what was published and keeps the byte-for-byte rule.

* `tools/security/rebuild_agent.sh <published> <out> <version> <ref>...` builds the Windows and Linux binaries from each
  ref's `agent/` (git archive, CGO off, `-trimpath`, `-s -w`, the version in `main.version`) and compares them with the
  published ones (SBOM files next to them are ignored); the first ref that reproduces both wins ("REBUILT FROM <ref>"),
  otherwise it prints every difference and fails. It refuses a missing binary, a version that is not digits and dots,
  and skips a ref that is not a commit.
* The workflow finds the oldest release whose assets include `skirnir-agent-<version>-linux-amd64` (tag names matching
  `v<digits>.<digits>.<digits>` only), checks out the history (`fetch-depth: 0`), and runs the script with the tag first and
  that release second. The script comes from the workflow's own commit (a sparse second checkout), so a manual run for a
  tag older than the script works. The step summary names the ref the binaries were rebuilt from.
* Attestation, SBOMs and upload are unchanged and still only follow an identical rebuild.

### Consequences

* Good, because router releases keep their agent attestation when `agent/` changed without a new agent.
* Good, because releases such as v0.35.1 can be attested by a manual run.
* Bad, because the attestation's workflow reference is the release's tag while the bytes were built from the earlier
  release's source; the step summary says which.
* Neutral: the agent's own update trust still rests on the operator's Ed25519 signature (ADR-0075).

### Confirmation

Locally, the v0.35.1 failure was reproduced: agent 0.35.1 built from v0.35.0's `agent/` (identical to v0.34.1's) as the
"published" binaries; the script with the tag v0.35.1 alone reported the Windows binary DIFFERENT and failed, as the CI
run of 2026-11-16 did, and with v0.35.1 then v0.35.0 it reported both identical and "REBUILT FROM v0.35.0". It refused a
missing binary, the version `0.35.1;x` and an unknown ref. The origin search, run read-only against the real releases,
found v0.34.1 for agent 0.35.1 and v0.36.0 for 0.35.2. `test/check_pinned_references.py` passes.
