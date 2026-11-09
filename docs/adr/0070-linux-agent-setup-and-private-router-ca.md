---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Linux agent setup and a private router CA

> Source: documentation review of the first installation; TM-07 in the threat model.

## Context and Problem Statement

On Windows the agent installs itself (`setup`, double-click). On Linux, `setup` only printed a hint, and the manual steps
lived in German comments of the example configuration with two traps: the node key's default folder is next to the
configuration under `/etc`, which the unit makes read-only, and the unit's writable folders had to exist beforehand.
Separately, the agent trusted only the operating system's certificate store for the tunnel and update downloads. A
homelab without a public DNS name - a router certificate from a private CA - had to install that CA system-wide on
every node.

## Decision Drivers

* The same one-step install on Linux as on Windows, checking before acting and never overwriting a configuration.
* Trust in a private CA scoped to the router connection, without weakening hostname verification.
* A wrong CA setting must fail at `check-config`, not fall back silently.

## Considered Options

* `skirnir-agent setup` on Linux (configuration with identity and log folders under `/var`, folders, binary, embedded
  unit, start) and `router.ca_file` (PEM added to the system roots for tunnel and downloads)
* An install shell script for nodes and a system-wide CA as documentation
* Certificate pinning by fingerprint instead of a CA file

## Decision Outcome

Chosen option: "Linux `setup` and `router.ca_file`", because it mirrors the Windows path in the binary itself and keeps
the extra trust to the one connection that needs it. A shell script would duplicate what the binary knows; pinning a
leaf fingerprint breaks on every certificate renewal.

* `sudo skirnir-agent setup [--router URL] [--update-key KEY] [--ca-file PEM]` refuses without root, a router address
  that is not https, a malformed key or an unusable CA file. It writes the configuration only if none exists (owner-only)
  with `identity.dir: /var/lib/skirnir-agent/identity` and `logging.dir: /var/log/skirnir-agent`, creates both folders
  (0700), copies the binary to `/usr/local/bin`, writes the unit embedded in the binary, validates the configuration,
  reloads systemd and starts the service, and asks to compare the fingerprint before approval. `--root DIR` stages the
  files without root or systemctl (packaging, tests).
* `router.ca_file`: the router connection trusts the system roots plus the certificates in that file; hostname
  verification stays on. A missing file or one without a certificate fails `check-config` and the start.
* The agent's usage text is English, and lists `game` (it said `spiel`).

### Consequences

* Good, because a Linux node is one command, without the read-only `/etc` trap.
* Good, because a homelab can use a private CA without changing every node's system trust.
* Bad, because a compromised CA file on a node lets that CA impersonate the router to it; the file needs the same care
  as the configuration.
* Neutral, because other platforms keep the manual steps.

### Confirmation

`agent/routertls_test.go`: no file means system defaults; missing or empty files are errors; a tunnel dial and an update
download to a server with a private certificate fail without and succeed with `ca_file`; `check-config` refuses a missing
file. `agent/setup_linux_test.go`: staged setup writes an owner-only configuration with the folders, 0700 folders, the
unit and an executable binary; a second run leaves the configuration unchanged; refusals write nothing; without `--root`
root is required.
