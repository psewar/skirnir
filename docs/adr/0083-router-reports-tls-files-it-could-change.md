---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# The router reports TLS files it could change, from its own view

> Source: follow-up to ADR-0073 after the v0.31.0 rollout on 2026-11-10: a deployment kept its certificate copy in
> another folder below `/etc/skirnir-router`, which the router unit could still write.

## Context and Problem Statement

ADR-0073 gave the router unit `ReadOnlyPaths=-/etc/skirnir-router/certs`, because root helpers (the ACME client, a copy
step in a certificate unit drop-in) write the certificate and key there. A compromised router that can change those
files, or rename the folder and put a symlink in its place, steers what a root process writes and where.

The protection only holds for that one folder. `control_tls` may name any path, and installations keep certificates
elsewhere for good reasons (an existing copy, a different ACME client). A deployment had its copy in
`/etc/skirnir-router/tls`: inside the router's `ReadWritePaths`, owned by root, written by a root `install` step, and
writable by the router, which runs as root without capabilities. Nothing reported it. ADR-0082 has since made `tls`
read-only for the unit as well, but any other folder below the router's writable folders stays open, and a unit
override without those lines opens `certs` and `tls` again, silently.

Whether the router can change a file is not a property of the configuration. It depends on the unit's mount namespace
(`ProtectSystem=strict`, `ReadWritePaths=`, `ReadOnlyPaths=`, drop-ins) and the credentials of the running process
(user, groups, an empty capability bounding set). `router.py --posture` runs outside the unit, as root with all
capabilities, and would see every path as writable.

## Decision Drivers

* The router states the fact itself, on every start, next to the other posture findings (ADR-0064); nobody has to
  remember which folder the unit protects.
* The check uses the view that matters: the running process's mounts and credentials, not a guess from the unit file.
* The unit protects the folders the installation itself uses (`certs`, ADR-0073; `tls`, ADR-0082); no list of further
  folder names can cover every installation.
* Findings are facts, not errors: the router still starts.

## Considered Options

* The running router checks its own view (`/proc/self/mountinfo`, owner and mode, capabilities) and reports
  `tls_files_changeable`
* Add further `ReadOnlyPaths=` entries for other common certificate folders to the router unit
* Refuse to start when a TLS file is writable
* Leave it to the documentation and to each deployment

## Decision Outcome

Chosen option: "the running router checks its own view", because it is the only place where the answer is exact, and it
covers any path `control_tls` names.

* `process_view.ProcessView.current()` reads the mount table and the effective capabilities of the process from
  `/proc/self` (Linux; elsewhere it reports no view and the check is left out).
* `changeable(path)` is true when the file lies on a writable mount and owner or mode let the process write it, or when
  the folder holding it, or any folder above it, lies on a writable mount and lets the process add or remove entries. A
  folder that is itself a mount point cannot be renamed or removed; a sticky folder protects entries of other owners;
  a symlink is checked as the link and as its target, and a folder reached through a link where it really lies. On a
  writable mount the owner counts as able to write whatever its mode says, because `chmod` needs only ownership.
  `CAP_DAC_OVERRIDE` and `CAP_FOWNER` count when the process has them. A relative path is judged from the working
  directory, where the router opens it; the walk always ends at the root, so the check cannot hold up the start.
* At start, `posture.report(cfg, view=...)` adds the medium finding `tls_files_changeable` (TM-14) for
  `control_tls.cert` and `control_tls.key`. Next step: keep the files in `/etc/skirnir-router/certs` or
  `/etc/skirnir-router/tls`, which the unit sees read-only, or outside the router's writable folders, or make their
  folder read-only for the unit in a drop-in. `router.py --posture` leaves the check out because it cannot see the view.

For the deployment that triggered this, the finding goes away once the unit from ADR-0082 is installed; its start log
confirms it (an operations step, not part of this repository).

### Consequences

* Good, because a certificate path the unit does not protect is visible in every start log, with the next step.
* Good, because the check also holds after the router moves to its own account: it then reflects that account's
  ownerships.
* Bad, because the finding only appears in the start log of the running router, not in `--posture` before a rollout.
* Bad, because the check models Linux permission rules (owner, group, mode, sticky bit, mount points); ACLs and
  security modules are not read. They can only take rights away, so the check may report a file that is in fact safe,
  never the other way round.

### Confirmation

`test/selftest_process_view.py` (CI) builds the router unit's mount table (read-only root, writable
`/etc/skirnir-router`, read-only `certs` and `tls`) and checks: files in `certs` and `tls` are safe, and the next step
names both; files in another folder below
`/etc/skirnir-router` are not; a folder owned by the process above a closed one can be swapped unless it is a mount point;
sticky folders, symlinks, group write, `CAP_DAC_OVERRIDE`, octal escapes and read-only superblocks; relative paths and a
walk that ends without an absolute path; the posture finding
with and without a view; and, on Linux, the view of the test process itself. In operation the start log of a deployment
with certificates outside `certs` and `tls` shows `tls_files_changeable`, and no longer after the move.

## Pros and Cons of the Options

### Check the running router's own view

* Good, because exact for the unit as deployed, including drop-ins.
* Bad, because only visible after a start.

### More ReadOnlyPaths entries in the unit

* Good, because it protects without any code; ADR-0082 does this for `tls`, the folder of older installations.
* Bad, because no list of folder names covers every installation, and a missed one stays silent.

### Refuse to start

* Good, because nobody can overlook it.
* Bad, because a router that will not start is a worse outcome than one that reports the risk; the posture findings are
  facts by design (ADR-0064).

### Documentation only

* Bad, because it is the weakest protection and exactly what failed here.

## More Information

Builds on ADR-0073 (router paths to host privileges), ADR-0082 (older certificate folder read-only) and ADR-0064
(security posture as facts). Threat model TM-14.
