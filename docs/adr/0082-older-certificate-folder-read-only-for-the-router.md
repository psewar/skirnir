---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# The router sees the older certificate folder read-only too

> Source: follow-up to the TM-14 review (ADR-0073), found while rolling it out on 2026-11-12. Complements ADR-0073.

## Context and Problem Statement

ADR-0073 keeps the router from changing what root processes read: its unit makes `/etc/skirnir-router/certs` read-only,
the folder `router.py --init`, both example configurations and `skirnir-router-cert.path` use. Installations set up
before `--init` (router 0.23.0) keep certificate and key in `/etc/skirnir-router/tls`. That folder lies inside
`ReadWritePaths=/etc/skirnir-router`, so a compromised router there can still replace its own TLS key and certificate,
the certificate the control port and the API port present to nodes and clients. Whether that also reaches root depends
on the ACME client: one that unlinks before writing (as `install` does) ignores a planted symlink, one that writes into
the existing file would follow it as root. ADR-0073 meant to rule out both for certificates.

The router never writes below `tls` either: it reads `control_tls` with `os.access(…, R_OK)` and `load_cert_chain`, and
every file it writes has a fixed name next to `config.yaml`, lives under `/var/lib/skirnir-router` and
`/var/log/skirnir-router`, or is a log path the operator sets. `router.py --init` only writes `config.yaml`. The certificate is written by the ACME client as
root, outside the router's unit.

## Decision Drivers

* The intent of ADR-0073 holds for every certificate folder in use, not only the one new installations get.
* No manual step for existing installations, and no change for hosts without that folder.
* The router keeps writing its own files in `/etc/skirnir-router`.

## Considered Options

* Add `ReadOnlyPaths=-/etc/skirnir-router/tls` next to the `certs` line
* Move existing certificates into `certs` during deploy
* Make all of `/etc/skirnir-router` read-only and list the router's writable files one by one

## Decision Outcome

Chosen option: "add `ReadOnlyPaths=-/etc/skirnir-router/tls`", because it closes the gap in the same deploy without
touching the certificates or the ACME client. The leading `-` keeps a host without that folder starting.

Moving the certificates would mean rewriting the ACME client's target, `control_tls` and a site-specific
`skirnir-router-cert.path` on hosts the repository does not know, and a failed move would stop TLS. A read-only
`/etc/skirnir-router` does not fit how the router writes there: it replaces `roles.yaml`, `nodes.json` and its JSON
state through a temporary file and a rename, which needs the folder writable; listing files would break with every new
state file. That change belongs with the dedicated router user that TM-14 still plans.

### Consequences

* Good, because the router cannot replace its certificate or key, or plant a symlink for the ACME client, in either
  folder an installation of this project uses, whatever the ACME client does.
* Good, because nothing changes for hosts without `tls`, and the ACME client and `skirnir-router-cert.path` work as before.
* Bad, because certificate and key in any other folder below `/etc/skirnir-router` stay writable for the router; the
  installation guide says to keep them in `certs`, `tls` or outside the router's writable folders.
* Bad, because a `certs` or `tls` that is a symlink stays replaceable: systemd mounts the link's target read-only, but
  the link is an entry in the router-writable `/etc/skirnir-router` (as for `certs` since ADR-0073). The guide asks for
  real folders; the dedicated router user removes this with the rest of the writable folder.
* Neutral, because systemd mounts the folder when the router starts: a `tls` folder created later is read-only from the
  next restart on, which certificate renewal triggers anyway.

### Confirmation

`test/selftest_privilege_paths.py` (CI) requires `ReadOnlyPaths=-/etc/skirnir-router/certs` and `-/etc/skirnir-router/tls`
in `skirnir-router.service`, checks that certificate and key under `tls`, the `--init` defaults, both example
configurations and the watched path of `skirnir-router-cert.path` are not router-writable under the unit (the most
specific path setting decides), and, as the rejected case, that the same unit without the `tls` line leaves certificate
and key writable. The router's own files in `/etc/skirnir-router` stay writable.
