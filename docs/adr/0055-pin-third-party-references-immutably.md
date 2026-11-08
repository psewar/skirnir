---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# Pin third-party references immutably

> Source: TM-16 in the threat model.

## Context and Problem Statement

CI and deployments fetched third-party code by movable names: most workflow actions by major tag (`actions/checkout@v7`),
`govulncheck@latest`, an unversioned `pip-audit`, container images by tag and Dockerfile base images by tag. A re-pushed
tag or a compromised publisher changes what runs without a change in this repository, and the security workflow only
audited the router's base requirements.

## Decision Drivers

* What runs must be what was reviewed; a change must show up as a diff.
* Updates must stay cheap, or pins rot into known-vulnerable versions.
* The rule must be enforced by CI, not remembered.

## Considered Options

* Pin actions to commit SHAs (with the release as a comment), tools to exact versions, images and base images to
  digests; Dependabot updates them; a CI check rejects movable references
* Keep tags and rely on Dependabot and audits (status quo)
* Vendor or mirror all third-party code

## Decision Outcome

Chosen option: "Pin, let Dependabot move the pins, enforce in CI", because it makes every change of fetched code a
reviewable diff at almost no maintenance cost. Mirroring would add infrastructure for a single-maintainer project.

* Workflow `uses:` of other repositories: full commit SHA; `go install`: a release version; tools installed with
  `pip install`: `==`; compose images: `tag@sha256:<digest>` (the tag stays readable); Dockerfile `FROM`: `@sha256`.
* `test/check_pinned_references.py` runs in CI; Dependabot covers GitHub Actions, the monitoring compose file and the
  decision-service Dockerfiles.
* `pip-audit` also audits the optional requirement sets (session store, development, memory tests, rerank server).
* Python application requirements keep version ranges; a hash-locked install is a separate decision.

### Consequences

* Good, because a re-pushed tag can no longer change CI, tooling or the monitoring stack unnoticed.
* Good, because known vulnerabilities in optional dependencies now fail the security workflow.
* Bad, because image and action updates arrive as Dependabot PRs that need a review record (images are outside the
  narrow exemption of ADR-0050).
* Neutral, because release artifact provenance (SBOM, attestations) stays open under TM-16.

### Confirmation

`test/selftest_pinned_references.py` rejects tags, branches, short SHAs, `@latest`, unpinned pip tools and images or base
images without digest, accepts the pinned forms, and checks the repository; CI runs both.
