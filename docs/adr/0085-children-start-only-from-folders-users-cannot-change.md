---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# Children start only from folders users cannot change

> Source: TM-14 follow-up after ADR-0073 (risk register: "warn when a LocalSystem agent child runs from a user-writable
> path").

## Context and Problem Statement

The agent starts its children (Ollama, a speech service) with its own rights: LocalSystem on Windows, root on Linux.
The Ollama installer for Windows is a per-user installer; it puts `ollama.exe` and its libraries under
`%LOCALAPPDATA%\Programs\Ollama`, which that user owns. A node that runs this installation as the agent's
child therefore lets any program running as that user - without elevation - replace `ollama.exe` or a library next to
it, and that code then runs as the system on the next child start. The same holds for a folder above that the user can
rename, and on Linux for a program owned by a user or writable by group or others.

Nothing reported this. The example configuration even pointed `cmd` at the per-user folder.

## Decision Drivers

* The agent states the fact itself, in its status and log, and acts on it; nobody has to audit folder permissions.
* Safe by default: a child that would hand out the system's rights does not start.
* A deliberate exception stays possible, visible with its reason (no silent switch).
* An update must not take a node's Ollama away: the self-update already runs `check-config` of the new binary first.
* The fix is a one-time copy into a protected folder; the router's Ollama updates replace files in place, so they keep
  working there.

## Considered Options

* Check before every start; refuse the child unless `children[].user_writable_reason` accepts it; `check-config`
  fails for a refused child, so the self-update keeps the running version
* Report only (status, log), always start
* Refuse to load the whole configuration
* Have the agent move Ollama or change the folder's permissions itself

## Decision Outcome

Chosen option: "check before every start, refuse unless a reason accepts it", because it closes the path by default,
names the next step, and keeps a node running rather than half-updated.

* `childExposure` resolves the child's command (`PATH`, symlinks) and asks who besides the system could change: the
  program (write, delete, permissions, owner), its folder (also add files: libraries, a renamed program), and every
  folder above (remove or rename entries, permissions, owner). Windows reads owner and DACL; SYSTEM, Administrators and
  TrustedInstaller are trusted, an untrusted owner counts (it may rewrite the DACL), inherit-only entries are skipped and
  deny entries ignored - the check may name someone a deny entry would stop, never the other way round. Linux reads owner
  and mode; a sticky folder protects entries of other owners.
* The supervisor checks before every start. Refused: the child is not started, the status shows `refused` with the path
  and who, and `exposure`; the log has one line per new reason; the check repeats every minute, so the child starts by
  itself once the folder is fixed. A failed check refuses too.
* `children[].user_writable_reason: "<why>"` accepts the exposure: the child starts, a warning with the reason is logged
  on every start, and the status keeps `exposure` with the reason.
* `check-config` prints `accepted: …` or `refused: … next: …` per exposed child and exits non-zero when any would be
  refused. The self-update runs it with the new binary before swapping, so a node keeps its running agent and Ollama, and
  the update order fails with that line as the reason.
* Documentation and the example configuration use `C:\Program Files\Ollama`. `agent\Move-OllamaProtected.cmd` moves an
  existing per-user Ollama child in one step: it asks for administrator rights itself, checks with the same rule who could
  change the child's program and the target, copies the program folder (the copy takes the target's permissions), points
  the child's `cmd` there, lets `check-config` accept it, restarts the service and checks that Ollama answers from the
  copy; otherwise it restores the configuration and removes a copy it made. `-Check` only reports; last line `STATE:`.

### Consequences

* Good, because a per-user Ollama no longer runs as the system unnoticed, and the default is the safe one.
* Good, because the reason for an exception is written where it applies and visible in every status.
* Bad, because existing nodes with a per-user Ollama child need an administrator step before agent 0.32.0 can install:
  `Move-OllamaProtected.cmd`, or `user_writable_reason`.
* Bad, because only the program and the folders on its path are checked, not every file below the program's folder (for
  example `lib\ollama`, which the copy into a protected folder covers) and not arguments that name scripts.

### Confirmation

* `agent/childpath_test.go`: a system program (`cmd.exe`, `/bin/sh`) is not reported; a program in a folder the test
  controls is; an unresolvable command is left to the start; `check-config` output and exit for refused, accepted and
  disabled children; the supervisor does not start an exposed child, records `refused`, and starts it once accepted.
* Manual check on a Windows node (2026-11-12): `check-config` with children in the per-user Ollama folder,
  `C:\Program Files\Git` and `C:\Windows\System32` refused only the first, naming the user as owner, and exited 1.
* `test/selftest_ollama_protected.ps1` (CI, PowerShell 7 on Linux and Windows PowerShell 5.1): the script's access rule
  (owner, rights per kind, inherit-only and deny entries, CREATOR OWNER), the checked path chain, and rewriting only the
  Ollama child's `cmd` (quotes, comment, CRLF, refusal without or with two `cmd` lines).

## Pros and Cons of the Options

### Report only

* Good, because nothing changes for running nodes.
* Bad, because the escalation stays open by default; a report nobody reads protects nothing.

### Refuse the whole configuration

* Bad, because the agent would stop serving and reporting altogether for one child.

### Move or re-permission automatically

* Good, because no administrator step.
* Bad, because the agent would change a user's installation and folders without being asked; surprising, and the tray
  app would keep updating the user copy.

## More Information

Threat model TM-14. ADR-0084 hardens the Linux unit. The service account option of the Windows installer
(`install.account`) remains available to run the whole agent without LocalSystem.
