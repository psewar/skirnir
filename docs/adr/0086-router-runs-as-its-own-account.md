---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# The router runs as its own account and writes only its state folder

> Source: TM-14 follow-up (risk register: "dedicated router user"). Supersedes ADR-0044 for the router, which kept root
> "until it has been tested on a real deployment". The Linux agent's unit is ADR-0084.

## Context and Problem Statement

The router ran as root without capabilities (ADR-0044) and wrote its own files next to `config.yaml` in
`/etc/skirnir-router`, which its unit therefore opened for writing. Root without capabilities is still uid 0: the owner
check passes for every file root owns. On a host running other services next to the router, a check from inside the
router's namespace on 2026-11-12 found 14 root-only files it could read: `/etc/shadow`, an SSH host key and the
environment and secret-store access files of the other services. A compromised router would have had the secrets of every
service on that host.

Writing into `/etc/skirnir-router` had a second cost. Root tools also write there (the ACME client's certificates, a
certificate copy, `render-env.sh`'s `secrets.env`, `deploy.py`'s releases), so every one of them had to guard against what
the router might have placed there (ADR-0073, ADR-0082, ADR-0083). A dedicated account that simply owned
`/etc/skirnir-router` would have kept that problem: the owner of a folder can rename and replace every entry in it.

## Decision Drivers

* The router reads nothing that only root may read and writes nothing a root process later trusts.
* Root tools never act on a path the router could have changed: no owner change, no write, no symlink to follow.
* Existing installations move by running the tools they already use (`deploy.py`, `install.sh`), with checks before any
  change and without losing state; development setups and tests keep working without the account.

## Considered Options

* Own account; the router writes only its state folder (`router.state_dir: /var/lib/skirnir-router`) and its log folder;
  `/etc/skirnir-router` stays root's and read-only for the router
* Own account that owns `/etc/skirnir-router`
* `DynamicUser=yes`
* Stay root without capabilities, hide more paths (`InaccessiblePaths=`, `TemporaryFileSystem=/opt`)

## Decision Outcome

Chosen option: "own account, state folder, configuration folder root's", because only it removes both the reading of
root-only files and the writable folder shared with root tools.

* Unit: `User=skirnir-router`, `Group=skirnir-router`; no `ReadWritePaths=` for `/etc/skirnir-router` (read-only under
  `ProtectSystem=strict`); `StateDirectory=` and `LogsDirectory=` 0700, which systemd hands to the account (it changes the
  owner of everything below them). `secrets.env` stays where it is, `root:skirnir-router` 0640: the router reads it as
  environment and as a file (`mqtt.password_file`), and root's `render-env.sh` may write there now that the router
  cannot place anything in that folder. The read-only lines for `certs` and `tls` stay for a drop-in that opens the
  folder again.
* `router.state_dir` (new key, absolute path): every file the router writes itself goes there - `roles.yaml`,
  `nodes.json`, `deletions.jsonl`, `events.jsonl`, `usage.json`, `metrics.json`, `perf.json`, `ollama-latest.json`,
  `requests.jsonl`. Without the key they stay next to `config.yaml` (development, tests). The router checks at start that
  the folder exists and is writable and refuses with its name otherwise. `config.minimal.yaml`, `config.example.yaml` and
  `router.py --init` set it.
* `router/router-layout.sh`, root tool, idempotent, `STATE:` line: creates the system account, refuses while
  `config.yaml` lacks the state folder (before anything changes), stops a router that still runs as root so it saves its
  files, moves them into the state folder (keeps a file already there, removes symlinks instead of following them), sets `/etc/skirnir-router` to `root:skirnir-router` 0750 with nothing writable for the group, and the
  certificate folders `certs` and `tls` to group-readable keys (0640). It refuses a configuration or certificate folder
  that is a symlink or not owned by root. `--tls` does only the certificates; `--check` reports.
* `install.sh` runs it with `--check` before changing anything and fully after installing; `deploy.py` checks the state
  folder in the local configuration before connecting and runs the tool before the restart; both check the configured
  files as the account (`setpriv --reuid`). `skirnir-router-cert.service` runs `--tls` before restarting the router, so a
  renewed key (root 0600 from the ACME client) stays readable for the group. `router.py --init` hands a new
  `config.yaml` to the group when the account exists.
* `render-env.sh` writes `secrets.env` through a temporary file in the same folder and hands it to the group.

### Consequences

* Good, because a compromised router reads only what its account and group may read: its configuration, its key, its own
  files and `secrets.env` - not other services' secrets, SSH keys or `/etc/shadow`.
* Good, because root tools no longer share a writable folder with the router; the guards of ADR-0073, ADR-0082 and
  ADR-0083 remain as a second line.
* Bad, because an installation needs `router.state_dir` in its configuration before the update; the tools refuse until
  it is there and say so. Operations tools that edited `/etc/skirnir-router/roles.yaml` on the host now find it in
  `/var/lib/skirnir-router` (`deploy.py --pull-roles` looks in both).
* Bad, because an ACME client that writes its key elsewhere than `certs` or `tls`, or through a symlink, needs its own
  step to make the key readable for the group; the router refuses to start with the file's name when it cannot read it.
* Bad, because the update stops a router still running as root a few seconds earlier than the restart would.

### Confirmation

* `test/selftest_state_dir.py` (CI): every written file follows `router.state_dir`, files of root tools stay next to
  `config.yaml`, a relative path is refused; `router-layout.sh --root` refuses without the key and changes nothing, moves
  the files, removes a planted symlink, keeps an existing file, leaves `secrets.env` in place, reports the layout as current on a
  second run, and refuses a symlinked configuration folder.
* `test/selftest_privilege_paths.py`: the unit runs as the account, cannot write `/etc/skirnir-router` but its state and
  log folders, reads `secrets.env` from the secret folder, `render-env.sh` writes it there; the certificate unit runs
  `--tls` before the restart; every file the router writes is in the tool's list of files to move (a new one without that
  entry fails).
* Staging on a systemd host (WSL Ubuntu, 2026-11-12): router from main installed and running as root, then this
  version: refused without `state_dir` with nothing changed; with it, the old router was stopped, `metrics.json` and
  `usage.json` moved, a planted `perf.json` symlink removed; the router ran as `skirnir-router`, answered, could read its
  configuration and key but not `/etc/shadow`, could write only its state and log folders; a second run reported the layout as current; a renewed key (root 0600) became `root:skirnir-router 0640` through
  the certificate unit and the router kept running; the environment from `secrets.env` reached the process.

## Pros and Cons of the Options

### Own account owning /etc/skirnir-router

* Good, because no code change: the router keeps writing next to `config.yaml`.
* Bad, because the owner of the folder can replace every entry, including what root tools write and read there.

### DynamicUser

* Good, because no account management.
* Bad, because the uid changes; files in `/etc` the router must read need a stable group, and the certificate step needs
  a name to hand the key to.

### Stay root, hide more

* Good, because no migration.
* Bad, because a list of hidden paths never ends; uid 0 still owns every root file it can reach.

## More Information

Threat model TM-14. Builds on ADR-0073 (paths to host privileges), ADR-0082 (older certificate folder read-only) and ADR-0083
(TLS posture). Supersedes ADR-0044 for the router.
