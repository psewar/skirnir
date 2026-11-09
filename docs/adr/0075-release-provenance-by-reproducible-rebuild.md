---
status: accepted
date: 2026-11-10
decision-makers: operator
---

# Release provenance by reproducible rebuild, with SBOMs

> Source: TM-16 next action "SBOM and provenance for release artifacts".

## Context and Problem Statement

The operator builds the agent binaries on a workstation (`git archive` of the release commit, `CGO_ENABLED=0`,
`-trimpath`), signs their update manifest with the operator key (ADR-0054) and uploads them to the GitHub release. The
signature tells an agent that the operator published these bytes; nobody could check that the bytes come from the tagged
source, and there was no list of what the binaries contain.

## Decision Drivers

* Anyone can verify a published binary against the repository without trusting the operator's workstation.
* Do not move the signing key into CI; the operator signature stays the update trust anchor.
* A difference between published and rebuilt bytes is visible, not silent.

## Considered Options

* Rebuild from the tag in CI, compare byte for byte, attest only identical bytes; attach CycloneDX SBOMs
* Build and sign releases in CI
* SBOMs only, without provenance

## Decision Outcome

Chosen option: "rebuild, compare, attest", because Go builds are reproducible with the same toolchain and flags, so CI
can confirm the operator's build without holding any key. Building and signing in CI would put the signing key on hosted
runners; SBOMs alone say what a binary should contain, not that it does.

* `.github/workflows/release-provenance.yml` runs when a release is published (or by hand with a tag): it downloads the
  `skirnir-agent-*` assets, takes the version from their names and the Go toolchain from their build information, rebuilds
  both from `git archive` of the tag with the release flags and `GOTOOLCHAIN=local`, and compares SHA-256. Identical:
  `actions/attest-build-provenance` attests the bytes, `actions/attest-sbom` attests each binary's SBOM, and the SBOMs are
  attached to the release. Different: the job fails and prints both hashes; nothing is attested.
* `tools/security/sbom.py` writes deterministic CycloneDX 1.6: per agent binary its hash, Go toolchain, build settings
  and every module with version and `go.sum` hash; for the router the locked Python packages with all their hashes and the
  distribution-provided requirements with their ranges.
* Verification: `gh attestation verify <file> --repo psewar/skirnir` (docs/operations.md).

### Consequences

* Good, because a compromised workstation or a swapped asset shows as a failed rebuild instead of going unnoticed.
* Good, because the SBOMs list the exact modules for vulnerability lookups.
* Bad, because a release whose agent assets came from an earlier commit than the tag fails the comparison; such releases
  need the workflow run against that commit, or stay unattested.
* Neutral, because the router is released as source; its SBOM lists dependencies, its provenance is the tag itself.

### Confirmation

`test/selftest_sbom.py`: CycloneDX header with an RFC 4122 serial, component hash and purl, Go toolchain, modules with
`go.sum`, replaced modules, build settings, determinism, refusal of wrongly named files; router SBOM with locked hashes
and distribution ranges. The workflow's comparison step fails on any differing byte; `check_pinned_references.py` covers
its actions.
