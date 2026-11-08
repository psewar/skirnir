---
status: accepted
date: 2026-11-08
decision-makers: operator
---

# Owner-only router files and secret canaries in CI

> Source: TM-15 in the threat model.

## Context and Problem Statement

The router writes an audit log, a decision journal, the node registry, UI overrides with client token hashes and
fallback observations. They carry client names, source addresses and model use, and they were created with the process
umask, typically world-readable. The rule that logs and audit contain no prompts, tokens or passwords was intent, not a
tested property.

## Decision Drivers

* Metadata about who asked what is personal data in a household or team.
* A rule that matters is enforced by a test, not by care.
* Deployments without systemd must be covered too.

## Considered Options

* Create every router-written file owner-only in code (and tighten existing ones), set `UMask=0077` in the unit, and
  run a canary test of the real request paths in CI
* Only `UMask=0077` in the systemd unit
* Redact at log level with a filter

## Decision Outcome

Chosen option: "Owner-only in code plus unit umask, canary test in CI", because the code path also covers installations
without the unit and tightens files created by earlier versions, while the umask covers anything written outside these
helpers. A log filter would hide leaks instead of revealing them.

* `common.open_private` creates files 0600 and tightens an existing file to 0600 where the platform has `fchmod`;
  all router file writes use it.
* `test/selftest_log_hygiene.py` drives the API and control apps in-process with canary values (wrong and valid client
  token, wrong and valid control password, a prompt, a token rotated through the admin API) and searches every captured
  log record, the audit log, the journal and the overrides file. It first checks that the events were recorded at all.

### Consequences

* Good, because local users without the service account can no longer read the router's metadata files.
* Good, because a new log line that leaks a token, password or prompt on these paths fails CI.
* Bad, because a separate reader of the audit log (for example a log shipper) now needs the service user or a group
  change.
* Neutral, because log rotation, backups of these files and independent audit retention stay open under TM-15.

### Confirmation

`test/selftest_log_hygiene.py` in CI (file modes checked on Linux); against the previous code the audit log, journal and
overrides file are reported as 0644.
