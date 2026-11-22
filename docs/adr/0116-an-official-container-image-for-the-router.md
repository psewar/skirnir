---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# An official container image for the router

> Part of the portability roadmap, phase 3 ([design/portability.md](../../design/portability.md)). Complements
> [ADR-0069](0069-router-install-script.md) (router install script) and keeps the hardening of
> [ADR-0086](0086-router-runs-as-its-own-account.md) (own account, state folder) and
> [ADR-0073](0073-close-router-paths-to-host-privileges.md); built under
> [ADR-0055](0055-pin-third-party-references-immutably.md), [ADR-0074](0074-hash-locked-python-installs.md) and
> [ADR-0075](0075-release-provenance-by-reproducible-rebuild.md). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

The router is installed only on Debian or Ubuntu with systemd: `router/install.sh` refuses other distributions (around
lines 52-55), uses fixed paths (`/opt/skirnir-router`, `/etc/skirnir-router`, lines 17-18; `/var/lib/skirnir-router`
and `/var/log/skirnir-router` in `router/router-layout.sh`) and relies on systemd units for hardening, the secret
renderer and certificate renewal. The repository ships Dockerfiles only for the optional decision services
(`decision-embed/Dockerfile`, `decision-jevlike/Dockerfile`) and a compose file for the monitoring stack
(`monitoring/compose.yaml`).

Many homelab users run services only as containers (Docker, Podman, Unraid, TrueNAS, Synology). For them, trying the
router means writing and maintaining their own image, with unpinned bases and none of the project's supply-chain
guarantees.

The router already suits a container in most respects: one Python process (ADR-0005), secrets by environment
variable reference (`mqtt.password_env`, `session_store.dsn_env`, `router.cloud.providers.<p>.api_key_env`), all written files in `router.state_dir`
(ADR-0086), configuration read-only.

## Decision Drivers

* Container users get a supported way that keeps the hash locks, pinning and provenance.
* No second configuration model: the same `config.yaml`, the same schema check.
* The container keeps the security properties of the systemd unit where a container can (no root, read-only
  configuration, only the state folder writable).
* The systemd install stays the reference path; nothing changes for it.

## Considered Options

* An official router image, built reproducibly in CI, with a compose example.
* Document how to build an image, ship no image.
* Leave containers to the community.

## Decision Outcome

Chosen option: "An official router image", because a reproducible, pinned image is the only way container users get the
same guarantees as a systemd install.

* Dockerfile in the repository: base image pinned by digest (checked by `test/check_pinned_references.py`), Python
  packages from `router/requirements-lock.txt` with `--require-hashes`, runs as a non-root user, configuration mounted
  read-only, `router.state_dir` as a volume, secrets only as environment variables (no renderer inside the image).
* Built in CI from the tag, with an SBOM and build provenance like the agent binaries; published by the maintainer with
  the release.
* Compose example with the optional PostgreSQL session store and the decision services, all off by default.
* The agent stays a native binary on the GPU node for now; an agent container mode is considered after the runtime and
  GPU probe interfaces (ADR-0111, ADR-0112).

### Consequences

* Good, because Docker, Unraid and TrueNAS users can run the router without writing their own image.
* Good, because the image inherits pinning, hash locks, SBOM and provenance.
* Bad, because the image is a second deployment path to test and document in both languages.
* Bad, because the systemd-only parts (secret renderer, certificate renewal unit, `ProtectSystem` and friends) have no
  direct equivalent; the documentation must say what the operator provides instead (secrets by environment, a reverse
  proxy or a mounted certificate).
* Bad, because the base image needs digest updates (Dependabot already proposes them for the existing Dockerfiles).

### Confirmation

CI builds the image on every PR that touches it and runs `router.py --check` on the example configuration inside it;
`test/check_pinned_references.py` covers its base image; the release workflow rebuilds it from the tag and attaches the
SBOM. A smoke test starts the container with a minimal configuration and checks `/api/version` and that it runs as a
non-root user with a read-only configuration.
