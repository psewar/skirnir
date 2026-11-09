---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Explicit agent key creation and lost-key recovery

> Source: TM-13 in the threat model; TM-07 for the approval step. Complements ADR-0058 (rotation with several keys).

## Context and Problem Statement

ADR-0058 made a planned rotation possible, but a lost signing key had no defined path. Worse, `deploy.py --agent` created
a signing key whenever none was in the ops folder: a lost or misplaced key silently became a new one, the release was
signed with it, and every node refused it without the operator learning why. The node-local edit of the trusted keys
lived only in an operator script outside the repository, and it was first written for PowerShell 7 while nodes run
Windows PowerShell 5.1. Approving a new node relied on comparing a fingerprint, without a written procedure.

## Decision Drivers

* A missing key is either a new installation or a lost key; only the operator knows which, so the tool must ask.
* Recovery must leave a record (when, why, which new key) without storing a secret there.
* The node-local steps belong next to the agent, work on Windows PowerShell 5.1, check themselves and roll back.

## Considered Options

* Explicit steps: `--create-agent-key` (first key), `--replace-lost-agent-key "<reason>"` (recovery, recorded),
  `--agent` refuses without a key before connecting; `agent/Set-UpdateKey.ps1` with Add, Complete, Replace and Check
* Keep creating the key in `--agent` and only document recovery
* A second, offline backup key trusted by every node from the start

## Decision Outcome

Chosen option: "Explicit steps and a node script in the repository", because it turns the silent failure into a refusal
that names both ways, and keeps recovery a deliberate act with a reason on record. A pre-trusted backup key is a useful
addition with ADR-0058 (list it in `update.public_keys`), but it does not remove the need for a defined recovery.

* `deploy.py --create-agent-key`: refuses if a signing or prepared key exists; writes the key owner-only from the start.
* `deploy.py --replace-lost-agent-key "<reason>"`: refuses without a reason of at least 8 characters, while the key still
  exists (then it is a rotation), or while a prepared next key exists (then `--use-next-agent-key`); otherwise writes a new
  key, `agent-update.recovery-<time>.txt` with time, reason and new public key, and prints the node step.
* `deploy.py --agent` refuses without a signing key before any connection and never creates one.
* `agent/Set-UpdateKey.ps1` (with `Set-UpdateKey.cmd` for the execution policy): Add / Complete (rotation), Replace (the
  new key becomes the only trusted key), Check. It checks administrator rights, the service, agent >= 0.29.0, the key
  format (before decoding), keeps a backup, runs `check-config`, restarts the service and restores on failure.
* Approving a node (TM-07): compare the fingerprint in the router UI with `skirnir-agent identity` on that node, over a
  channel other than the network the agent enrolled through.

### Consequences

* Good, because a lost key can no longer hide behind a new one, and recovery has a record.
* Good, because the node steps are versioned with the agent and tested on Windows PowerShell 5.1.
* Bad, because after a lost key every node needs an administrator step before it accepts a release again.
* Neutral, because Linux nodes edit `update.public_key` by hand; the script is for Windows nodes.

### Confirmation

`test/selftest_key_rotation.py`: first key only on an empty folder and owner-only; the replacement refuses without a
reason, while the key exists and with a prepared key, then creates a new key and a record without the secret; `deploy.py`
checks the key before connecting and never creates one. `Set-UpdateKey.ps1` checked on Windows PowerShell 5.1 with the
real agent binary: Add, repeated Add, Complete, Complete without Add refused, Replace, invalid key refused, no BOM.
