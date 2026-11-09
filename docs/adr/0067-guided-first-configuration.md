---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Guided first configuration

> Source: documentation review of the first installation; TM-01 and TM-03 in the threat model.

## Context and Problem Statement

A new operator started from `router/config.example.yaml`: 400 lines describing one homelab, four clients sharing the
token "changeme" with every role, a decision engine and cloud providers that need further services. Copied as is, it
loaded, served the example token and exposed the control port; adapting it meant six or more edits by hand, a password
hash via `router.py --hash '<password>'` (which lands in the shell history) and a client token hashed by hand. The posture
report (ADR-0064) did not flag a well-known token or a control port without TLS.

## Decision Drivers

* The first configuration must be valid and safe without reading the full reference.
* Secrets are generated or asked without echo, stored only as hashes, shown once.
* The tool checks its preconditions and never overwrites an existing configuration.

## Considered Options

* `router/config.minimal.yaml` (one role, one model, enforce and locked, TLS on, no cloud, MQTT or decision engine) and
  `router.py --init <dir>` that fills it in; posture findings for example tokens and a control port without TLS
* An interactive wizard that asks for every option
* Only a shorter example file

## Decision Outcome

Chosen option: "Minimal template plus `--init`", because it removes every manual secret step while staying a plain file
the operator can read and extend. A full wizard would duplicate the reference and age with every option; a shorter file
alone still leaves secrets to be made by hand.

* `router.py --init <dir> [--public-url] [--user] [--client] [--cert] [--key]` refuses if `<dir>/config.yaml` exists, the
  template lacks a marker, the password is shorter than 12 characters (or differs from its repetition on a terminal),
  a name is not plain or the URL is not https. It writes the file owner-only and exclusively, loads it like `--check`,
  prints the posture report and the client token once, and ends with `INIT: written` or `INIT: refused`.
* `router.py --hash` without an argument asks for the password without echo; `router.py --help` lists the commands; an
  unknown option is refused instead of being read as a configuration file.
* New posture findings: `client_example_token` (high: sha256 of a well-known placeholder such as "changeme"),
  `control_without_tls` (high: control port on a network address without `control_tls`), `control_on_network` (info:
  firewall it to nodes and admin machines).

### Consequences

* Good, because a first installation has no placeholder secret and starts with no high posture finding.
* Good, because a configuration copied from an example is now reported, not silently trusted.
* Bad, because `config.minimal.yaml` is a second example to keep in sync; `selftest_init.py` loads it on every CI run.
* Neutral, because the full `config.example.yaml` stays the reference for every option.

### Confirmation

`test/selftest_init.py`: the CLI writes an owner-only configuration that loads, verifies the password hash, stores the
token only as sha256 and shows it once, has no high or medium finding; refusals leave files untouched; the template loads
and its placeholders are reported. `test/selftest_posture.py`: the new findings.
