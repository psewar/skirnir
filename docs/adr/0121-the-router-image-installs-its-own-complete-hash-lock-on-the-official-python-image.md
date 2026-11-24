---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# The router image installs its own complete hash lock on the official Python image

> Refines [ADR-0116](0116-an-official-container-image-for-the-router.md) (official router image, proposed in #177) for
> how the image gets its Python and its packages and which account it runs as. Keeps
> [ADR-0055](0055-pin-third-party-references-immutably.md) (digest-pinned images),
> [ADR-0074](0074-hash-locked-python-installs.md) (hash-locked installs) and
> [ADR-0086](0086-router-runs-as-its-own-account.md) (own account). Design: [design/router-container.md](../../design/router-container.md).

## Context and Problem Statement

ADR-0116 says the image installs its packages from `router/requirements-lock.txt` with `--require-hashes`. That lock is
not a complete environment: it holds only what pip may add **on top of** a Debian or Ubuntu host's packages
(`asyncpg`, `paho-mqtt`, `uvloop`), and `router/skirnir_router/pydeps.py` installs just the entries a host lacks, with
`--no-deps`. aiohttp, PyYAML and cryptography and their dependencies come from apt on a host. An image needs all of
them from somewhere, reproducibly, and needs a fixed account that volume owners can be set for.

## Decision Drivers

* Every Python package in the image is pinned to an exact version and checked against its hash; nothing is resolved
  at build time.
* Rebuilding the same commit gives the same packages; a base image update arrives as a reviewable digest change.
* Hosts keep their behaviour: the systemd install and its lock do not change.
* The image works on amd64 and arm64 (Raspberry Pi, many NAS boxes) without a compiler.
* Operators can give bind-mounted folders to the router's account without knowing image internals.

## Considered Options

* Official `python:<version>-slim` image, pinned by digest, plus a separate lock with the router's complete dependency
  closure, installed whole (`--require-hashes --no-deps --only-binary=:all:`).
* `debian:<release>-slim` plus apt packages (`python3-aiohttp` …) like a host, plus `requirements-lock.txt` through
  `pydeps.py`.
* Extend `router/requirements-lock.txt` to the full closure and let the image use it.

## Decision Outcome

Chosen option: "official Python slim image plus a separate complete lock", because it is the only one where every
package version is fixed by the repository: apt installs whatever the archive serves on the build day, and a full
closure in the host lock would change what hosts may install from pip instead of from their distribution.

* Base image `python:3.15-slim-trixie@sha256:…` (`router/Dockerfile`); its Python must be one that
  `tools/security/lock_requirements.py` checks the locks for (`PYTHONS`), which `test/selftest_container_image.py`
  enforces.
* `router/requirements-container-lock.txt`: the router's requirements (`requirements-session.txt`) with every transitive
  dependency, written by `lock_requirements.py pin` (which refuses an incomplete closure) with every hash PyPI
  publishes, so one lock serves amd64 and arm64. Versions are those of `requirements-ci-lock.txt`, so the image runs
  what CI tests; the self-test checks that. `lock_requirements.py check` and `verify-pypi` cover the new lock.
* The build installs it whole, wheels only, then checks the result (`pydeps.py verify`, `pip check`, imports) and
  removes pip, so the running image cannot install packages and carries no unused installer.
* The router runs as `skirnir-router` with the fixed uid and gid 10001 (above the ranges distributions give to system
  and login accounts, so it collides with no host account by accident). The configuration folder is `root:10001 0750`,
  the state and log folders `10001:10001 0700`, as `router-layout.sh` sets them on a host.

### Consequences

* Good, because the image's packages are fully determined by two files under review (the Dockerfile digest and the
  lock), on both architectures.
* Good, because hosts are untouched: `requirements-lock.txt`, `pydeps.py plan` and `install.sh` work as before.
* Bad, because there is a third lock to refresh; a version bump in `requirements-ci-lock.txt` now also needs a `pin`
  run for the container lock, or the self-test fails.
* Bad, because a host on Debian 12 runs Python 3.11 while the image runs 3.15; CI's self-tests run on 3.11, so the
  container job also runs the router self-tests inside the image.
* Bad, because uid 10001 is a convention operators must use for bind mounts (`chown`); named volumes get it
  automatically.

### Confirmation

`test/selftest_container_image.py` (base digest, locked Python, install flags, uid, versions equal to the CI lock,
lock covers every requirement on every supported Python); `lock_requirements.py check` in CI; the CI job `container`
builds the image, starts it and runs the router self-tests inside it (`test/smoke_container.sh`).
