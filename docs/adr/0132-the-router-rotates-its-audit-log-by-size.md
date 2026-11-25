---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The router rotates its audit log by size

> Threat: TM-15 (next action "log rotation and retention"). Complements
> [ADR-0057](0057-bound-unauthenticated-statistics-and-audit-volume.md) (the audit volume per source and per minute)
> and [ADR-0056](0056-owner-only-router-files-and-log-canaries.md) (owner-only files).

## Context and Problem Statement

The router appends every security event (refused and limited requests, authentication failures, configuration and
node actions) to `client_auth.audit_log`, by default `/var/log/skirnir-router/audit.jsonl`. ADR-0057 bounds how fast it
grows under attack, but nothing bounds how large it gets: the file grows for the life of the installation until the log
partition is full, and then audit lines are lost (the router continues and logs only to the journal). The other files
the router appends to are already bounded: the decision journal compacts itself, the fallback observations rotate at a
fixed size. The installation ships no logrotate configuration, and the container image has no logrotate at all.

## Decision Drivers

* The audit log must not fill the disk, also in the container and without external tools.
* An existing configuration keeps working unchanged (AGENTS.md): deleting old audit lines must not start silently.
* The router opens the file for every line, so a rename takes effect at the next line without signals.
* Rotated files stay owner-only, like the log itself.

## Considered Options

* **Size rotation in the router, opt-in per configuration: `client_auth.audit_log_max_mib`, `audit_log_keep`.**
* Ship a logrotate configuration with `install.sh`. Rejected as the only control: it covers only the default path on
  hosts installed with `install.sh`, not a configured path or the container; operators may still use logrotate
  instead (the router reopens the file per line, so renaming works).
* Rotation on by default. Rejected: it would start deleting audit lines on existing installations without an operator
  decision; the example and init configurations set it instead, and the changelog asks existing installations to.
* Time-based rotation. Rejected: size is what fills the disk; time would need a schedule and still no size bound.

## Decision Outcome

Chosen option: "size rotation in the router, opt-in", because it bounds the file wherever the router runs and leaves
existing installations as they are until the operator sets a limit.

* `client_auth.audit_log_max_mib` (integer 0-4096; 0 or absent = no rotation) and `client_auth.audit_log_keep`
  (integer 1-100, default 5). The configuration refuses other values.
* Before a line is written, `auth.rotate_audit_log` checks the file: once it has reached the limit it becomes
  `<path>.1`, `<path>.<n>` becomes `<path>.<n+1>`, and `<path>.<keep>` is removed first. The new line opens a new file
  with `open_private` (0600). A file system error is handled like a write error before: the event still reaches the
  journal.
* `config.example.yaml` and `config.minimal.yaml` (the `router.py --init` configuration) set 64 MiB.

### Consequences

* Good, because the audit log is bounded at about (keep + 1) x limit wherever the router runs.
* Good, because nothing changes for an existing configuration until the operator chooses a limit.
* Bad, because audit lines older than the kept generations are deleted once a limit is set; keeping them longer needs
  a larger `keep`, a backup or forwarding to an independent store (TM-15's remaining action).
* Bad, because an existing installation stays unbounded until its configuration gets the key; the changelog says so.
* Bad, because whoever can produce many audit lines pushes older ones out sooner. ADR-0057 caps the unauthenticated
  volume per source and per minute, every line also goes to the journal (`AUDIT ...`), and an independent store remains
  the answer for evidence that must outlive the router's files.

### Confirmation

`test/selftest_audit_rotation.py` (in CI): without a limit the file only grows; with 1 MiB and keep 3, five fills
leave the current file plus `.1` to `.3`, `.1` the newest, the oldest removed, the line after a rotation in a new file;
new and rotated files are 0600 (POSIX); the configuration refuses values outside the ranges, strings, floats and
booleans (rejected cases).
