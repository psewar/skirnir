---
status: accepted
date: 2026-09-26
decision-makers: operator
---

# Ollama runs as a child of the agent and is updated through the router

> Recorded retroactively on 2026-10-30; decided on 2026-09-26 (v0.9.0: router 0.3.0, agent 0.12.0), swap only after the
> router's release since 2026-10-01 (v0.11.0). Source: README "Updating Ollama", CHANGELOG.

## Context and Problem Statement

A node must be ready after a reboot without anyone logging in. When Ollama runs as a child of the agent service, the tray
app's own updater no longer applies (it runs under the desktop user, the installer is per user).

## Decision Drivers

* Nodes serve after a reboot without a login.
* Ollama stays current without manual work, outside busy hours.
* No request is cut off by an update; a bad version rolls back.

## Considered Options

* Ollama as an agent child; the router checks for new releases and names the version; the agent downloads from its own
  fixed source, verifies against the release's checksums, reports ready, and swaps only after the router has taken the
  node out of routing; night window, canary first
* Leave updates to the tray app
* The router pushes the files

## Decision Outcome

Chosen option: "Agent child, router names the version, agent fetches and verifies", because the router never chooses the
code, only the version, and the swap waits until no request runs (v0.11.0). The agent reports whether Ollama is its child
(`ollama_managed`), and the router offers updates only for managed nodes (ADR-0022).

### Consequences

* Good, because updates happen at night, canary first, with rollback if the new version does not answer.
* Bad, because only Windows archives are handled so far.

### Confirmation

Agent tests for download, checksum, swap and rollback; the router's update loop logs each step in the journal.
