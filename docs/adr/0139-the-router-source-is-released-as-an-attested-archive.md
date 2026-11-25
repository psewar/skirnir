---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The router source is released as an attested archive

> Extends [ADR-0075](0075-release-provenance-by-reproducible-rebuild.md) (release provenance by reproducible rebuild)
> to the router. Threat: TM-16 (next action "consider signing the router source archive").

## Context and Problem Statement

Releases carry the agent binaries, and the workflow *Release provenance* rebuilds them from the tag and attests their
build provenance (ADR-0075). The router is released as source: an operator clones the tag or downloads GitHub's
automatic source archive and runs `router/install.sh`. Neither carries an attestation, so nothing ties the router files
an operator installs to the repository's tag in a way they can verify offline from the download. GitHub's automatic
archives are generated on request and are not release assets one can attest.

## Decision Drivers

* An operator can verify the router files they install, as they can for the agent binaries.
* No new signing key to keep: the attestation uses the workflow's identity, as for the agent.
* The router's attestation must not depend on the agent rebuild (a router-only release, or an agent difference, must
  not leave the router unattested).

## Considered Options

* **A second job in *Release provenance*: `git archive` of `router/` at the tag as
  `skirnir-router-<version>.tar.gz`, attested with `actions/attest-build-provenance` and attached to the release.**
* Sign the archive with the operator's Ed25519 update key. Rejected: that key stays offline for agent manifests
  (ADR-0058); using it for every release widens its exposure.
* Attest GitHub's automatic source archive. Rejected: it is produced on download, not a release asset, and its bytes are
  not guaranteed stable.

## Decision Outcome

Chosen option: "an attested archive from a separate job", because it gives the router the same verifiable provenance as
the agent without a new key and independent of the agent rebuild.

* Job `router-source` (on release published or by hand with a tag): checks out the tag without credentials, reads the
  router version from `common.py`, writes `skirnir-router-<version>.tar.gz` with `git archive --prefix=skirnir-router-
  <version>/ HEAD router`, attests it and uploads it to the release (`--clobber`, so a manual re-run replaces it).
* `docs/operations.md` shows `gh attestation verify skirnir-router-<version>.tar.gz --repo psewar/skirnir` and how to
  install from the archive.

### Consequences

* Good, because the installed router files can be traced to the tag by anyone with `gh`.
* Good, because a failing agent rebuild no longer leaves the release without any attested router artefact.
* Bad, because the attestation covers the archive's bytes, not a reproducible rebuild of them; it proves origin from the
  workflow at that tag, not that a second build gives the same bytes.
* Neutral: installing from a clone stays possible and unattested.

### Confirmation

The job uses only actions already pinned by commit (ADR-0055; `test/check_pinned_references.py`). Locally, the same
`git archive` command produced an archive whose `router/` folder holds every file `install.sh` requires; the release
workflow itself runs only on a published release, so its first real run is the next release.
