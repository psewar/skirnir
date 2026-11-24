---
status: proposed
date: 2026-11-24
decision-makers: maintainer (proposed by Claude)
---

# CI builds the decision service images when they change and weekly

> Complements [ADR-0055](0055-pin-third-party-references-immutably.md) (Dependabot updates the digest-pinned base images)
> and [ADR-0116](0116-an-official-container-image-for-the-router.md) / [ADR-0121](0121-the-router-image-installs-its-own-complete-hash-lock-on-the-official-python-image.md)
> (the router image, which CI already builds). Design: [design/decision-engine.md](../../design/decision-engine.md).

## Context and Problem Statement

Dependabot updates the base images of `router/`, `decision-embed/` and `decision-jevlike/` (`.github/dependabot.yml`,
ecosystem docker). CI builds the router image (job `container`), but nothing built the two decision service images.
Their Dockerfiles install the newest packages at build time (`numpy onnxruntime tokenizers`; `torch` from the PyTorch
CPU index and `jevlike` from a pinned commit archive).

Dependabot proposed Python 3.15 for `decision-embed` twice (#150 on 2026-11-15, #173 on 2026-11-24). Both pull requests
looked green apart from the security evidence, but the image could not be built: onnxruntime publishes no wheel for
Python 3.15. Each time a person found it by building the image by hand. `decision-jevlike` moved to Python 3.15 without
any build check (it does build, as the first run of this check showed).

## Decision Drivers

* An update that breaks an image shows on its pull request, before the merge, without a person building it by hand.
* The same mistake happened twice, so the check belongs in the tooling, not in a rule (AGENTS.md).
* No ~1 GB torch image build for every unrelated pull request; no new third-party action.
* What the check covers is derived from the repository (Dependabot's list, the Dockerfiles), not a list that can drift.

## Considered Options

* **A separate workflow, triggered by the files that go into the images and weekly, running a script that also runs
  locally, plus a self-test that keeps it in step with Dependabot's list.**
* A job in `ci.yml` on every pull request. Rejected: it builds the torch image for every change, and jobs in one
  workflow cannot be filtered by path without a third-party action.
* A wheel-availability check against PyPI. Rejected: it repeats pip's resolution (markers, the PyTorch index) and misses
  what only a build shows (a missing `COPY` source, building `jevlike` from its archive, an import that fails).
* Dependabot ignore rules for Python versions per directory. Rejected: they hide real updates, must be removed by hand,
  and do not catch a package release that breaks an image.
* Hash-locking the decision images like the router (ADR-0121). Not decided here: it makes builds reproducible but is a
  larger change; the weekly build would then test the locked set.

## Decision Outcome

Chosen option: "a separate workflow with a local script and a self-test", because it catches both triggers (a base image
bump on its pull request, a breaking package release within a week) at the cost of a build only when it matters.

* `test/smoke_decision_images.sh [--engine docker|podman] [SERVICE ...]` builds each image from its directory as its
  `compose.yaml` does. Inside the image, without network, it compiles every program file in `/app` on the image's Python
  and loads every module those files import from outside `/app`, with the names they take from it. It derives the
  imports from the files (`ast`), so it follows changes to the services. No service code runs: both servers load their
  model while they start, and the models are not in the repository. Last line `STATE: passed | failed | refused`.
* `.github/workflows/decision-images.yml` runs it per service (matrix) on pull requests that change a Dockerfile, a file
  a Dockerfile copies, the script or the workflow, weekly, and on demand; read-only permissions, pinned checkout.
* `test/selftest_service_images.py` (in `ci.yml`, offline) fails when a directory Dependabot updates is not built in CI
  (the router by the `container` job, every other by this workflow and script), when a Dockerfile or a file it copies
  is missing from the trigger paths, or when the schedule or the read-only permissions go away.

### Consequences

* Good, because a Dependabot base image bump that breaks a decision image is a red check on its pull request, and a
  package release that breaks one shows in the weekly run.
* Good, because the same script runs locally with Docker or Podman.
* Bad, because a path-filtered workflow cannot be a required check (it does not run on unrelated pull requests); it
  gates like any red check under AGENTS.md ("do not merge with failing CI").
* Bad, because the check neither starts a service nor loads a model: an incompatibility that only shows at runtime (an
  ONNX operator, a checkpoint format) is not covered.
* Bad, because a weekly failure shows in the Actions tab and its notification, not on a pull request.

### Confirmation

`test/selftest_service_images.py` in CI (see above, with rejected cases for each rule). The check was shown to fail for
`decision-embed` on the base image of #173 (onnxruntime has no wheel for Python 3.15) and to pass for both services on
`main` (2026-11-24).
