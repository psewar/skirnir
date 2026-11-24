# Router container image

How the router runs as a container. The reasons are in
[ADR-0116](../docs/adr/0116-an-official-container-image-for-the-router.md) (an official image) and
[ADR-0121](../docs/adr/0121-the-router-image-installs-its-own-complete-hash-lock-on-the-official-python-image.md)
(base image, lock and account); operator steps are in [docs/install-router.md](../docs/install-router.md#container).

## One program, two ways to install it

The image contains the same files `router/install.sh` puts into `/opt/skirnir-router` and uses the same paths, so one
`config.yaml` works for both and every command (`--init`, `--check`, `--posture`, `--hash`) behaves the same:

| Path | systemd install | Container |
|---|---|---|
| `/opt/skirnir-router` | program files, root's, `ReadOnlyPaths=` | program files in the image, root's |
| `/etc/skirnir-router` | `root:skirnir-router 0750`, read-only for the unit (`ProtectSystem=strict`) | the operator's folder, mounted read-only |
| `/var/lib/skirnir-router` | `StateDirectory=`, `router.state_dir` | volume, `10001:10001 0700` in the image |
| `/var/log/skirnir-router` | `LogsDirectory=`, `client_auth.audit_log` | volume, `10001:10001 0700` in the image |
| router log | journal (stdout) | container log (stdout) |
| account | `skirnir-router`, dynamic system uid | `skirnir-router`, uid/gid 10001 |

`test/selftest_container_image.py` derives the list of program files from `install.sh` and the folders from
`router-layout.sh` and `config.minimal.yaml`, so the two cannot drift apart unnoticed.

## Build

`router/Dockerfile`, build context `router/`:

1. `FROM python:3.15-slim-trixie@sha256:…` - digest-pinned (ADR-0055), updated by Dependabot.
2. `pip install --require-hashes --no-deps --only-binary=:all: -r requirements-container-lock.txt`: every package of the
   router's dependency closure at an exact version, each file checked against its SHA-256, no build from source, no
   resolution (ADR-0074, ADR-0121). Then `pydeps.py verify` against `requirements-session.txt`, `pip check` and an import
   of every package; then pip is removed.
3. The account `skirnir-router` (uid/gid 10001) and the three folders with the owners and modes `router-layout.sh` sets
   on a host.
4. The program files, owned by root; `container/entrypoint.sh` and `container/healthcheck.py`.

No secret, configuration or certificate is in the image; `.dockerignore` keeps local data out of the build context.

## Run

- **Entry point** `container/entrypoint.sh`: `umask 077` (the unit's `UMask=0077`, ADR-0056), then
  `exec python router.py "$@"`. The default argument is `/etc/skirnir-router/config.yaml`; any other argument is a
  router command, e.g. `--init /etc/skirnir-router`.
- **First configuration**: `--init` runs as root in a one-off container with the configuration folder mounted writable.
  It finds the group `skirnir-router` and hands `config.yaml` to it (`root:10001 0640`) as on a host
  (`setup_init.readable_for_router`). The running container mounts the folder read-only and runs as uid 10001.
- **Secrets** come as environment variables the configuration names (`*_env` keys) or as files in the mounted folder.
  There is no secret renderer in the image; the operator provides the environment (`env_file`, or the orchestrator's
  secret mechanism).
- **Health**: `container/healthcheck.py` reads `router.listen`, `router.api_tls` and `router.control_tls` from the same
  `config.yaml` and asks `GET /api/version` on loopback (http or https, the certificate is not verified because it
  names the public host). `/api/version` needs no identity (`auth.EXEMPT_PATHS`), so the probe carries no secret. It
  prints `HEALTH: ok <version>` or `HEALTH: failed <reason>` and exits 0 or 1. The image declares it as `HEALTHCHECK`;
  `compose.yaml` repeats it because Podman's default OCI image format drops `HEALTHCHECK`.
- **Hardening** in `compose.yaml`: `user: 10001:10001`, `read_only: true`, `cap_drop: [ALL]`,
  `no-new-privileges`, a small `/tmp` tmpfs, configuration `:ro`. This is the container's counterpart of
  `NoNewPrivileges=`, `CapabilityBoundingSet=`, `ProtectSystem=strict` and `ReadOnlyPaths=` in the unit.
- **Posture** (ADR-0064, ADR-0083): `ProcessView` reads `/proc/self/mountinfo` and `CapEff` as on a host. A
  configuration folder mounted read-only is a read-only mount, so certificates there are not reported as changeable;
  with no capabilities and uid 10001 nothing above them is writable either. The container itself adds no finding; the
  usual `control_on_network` (info) appears because the router listens on all addresses inside the container.

## What the systemd install has and the container leaves to the operator

| systemd install | Container |
|---|---|
| `render-env.sh` + timer render `secrets.env` from a secret store and restart the router | `env_file` or the orchestrator's secrets; `docker compose up -d` after a change |
| `skirnir-router-cert.path` restarts the router after a certificate renewal | renew on the host into `config/certs/`, then `docker compose restart router` |
| `router-layout.sh` sets owners and modes, `install.sh` checks with `--check --files` as the account | the operator gives the configuration folder to group 10001; `docker compose run --rm router --check /etc/skirnir-router/config.yaml --files` checks as the account |

## Optional services

`compose.yaml` keeps them behind profiles, so a plain `docker compose up -d` starts only the router: `sessions`
(PostgreSQL for `router.session_store`, its password in its own `postgres.env`) and `decision` (the embedding decision
service, a locally built `skirnir-embed` image). Images from a registry are digest-pinned; locally built ones have
`pull_policy: never`, so a missing local image is an error instead of a pull from a registry.

## Tests

- `test/selftest_container_image.py` (CI python job): the files above, without an engine.
- `test/smoke_container.sh` (CI job `container`, also local with Docker or Podman): builds the image, runs `--check` on
  the example configuration, `--init` into a mounted folder, writes a throwaway certificate
  (`test/smoke_container_cert.py`), starts the container as `compose.yaml` does and checks the health probe,
  `/api/version` over the published port, uid 10001 without capabilities, a read-only configuration and program folder,
  a writable state folder, the posture report and a log without errors. `--selftests` runs the router's self-tests
  inside the image, on its Python and packages. It runs on Linux or inside WSL; under Git Bash (MSYS2) or Cygwin, which
  rewrite the container paths it passes, it refuses before building anything and names the `wsl` command instead.

## Not yet

- **Publishing.** CI builds and tests the image but pushes it nowhere. Publishing to a registry, with an SBOM and build
  provenance from the release workflow (ADR-0116), is the maintainer's release decision; until then operators build
  the image from a release or clone.
- **Agent container.** The agent stays a native binary on the GPU node (ADR-0116).
