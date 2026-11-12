---
status: proposed
date: 2026-11-12
decision-makers: maintainer
---

# Node scripts keep their logs outside the script folder

> Source: repository review of 2026-11-12; refines the log location in ADR-0072 (`Set-OllamaLoopback`) and ADR-0085
> (`Move-OllamaProtected`), which both said "a log is written next to the script".

## Context and Problem Statement

`agent/Set-OllamaLoopback.ps1` and `agent/Move-OllamaProtected.ps1` re-launch themselves elevated (UAC) and record the
elevated run with `Start-Transcript`, so the operator can read afterwards what the run saw and changed. The transcript was
written next to the script, named `<script>-<computer>-<timestamp>.log`.

Operators run these scripts from a repository checkout. The transcript then lands in `agent/` inside the working tree. It
names the computer, Windows user accounts, the agent's configuration path, firewall rules and the addresses of machines
connected to Ollama. `.gitignore` did not cover it, so one `git add -A` puts device identifiers into a public commit,
which the repository's public-data rules forbid. The review found exactly such a file in a checkout.

## Decision Drivers

* A run from a checkout must not leave identifying content in the working tree.
* The transcript stays readable for the operator, and only for administrators: it describes the node's setup.
* The location is derived, not asked for; the operator learns where the file is without searching.
* Works on a node without the agent too (`Set-OllamaLoopback` also handles a desktop-app Ollama).

## Considered Options

* The agent's log folder (`logs` next to its configuration), else the TEMP folder of the elevated account
* The folder named by `logging.dir` in the agent configuration
* Always the TEMP folder of the elevated account
* Keep the folder of the script and rely on `.gitignore`

## Decision Outcome

Chosen option: "the agent's log folder, else TEMP", because it is the place operators already look for the agent's logs,
and the agent itself restricts it.

* The scripts read the configuration path from the command line of the `SkirnirAgent` service and write to `logs` next to
  it - the agent's default log folder, `C:\ProgramData\skirnir-agent\logs` on a standard install. The agent restricts
  that configuration folder to SYSTEM, Administrators and its service account (`applyRules`), and `logs` inherits it.
* Without the service, with a command line that names no configuration folder, a configuration at a drive root, or the
  folder missing, the transcript goes to the TEMP folder of the elevated account.
* Both scripts start the transcript through one helper, `Start-RunTranscript` in `Set-OllamaLoopback.ps1`, which
  `Move-OllamaProtected.ps1` dot-sources. The line `Log: <file>` before the final `STATE:` line names the file; the
  non-elevated caller says where to look.
* `.gitignore` ignores `agent/Set-OllamaLoopback-*.log` and `agent/Move-OllamaProtected-*.log` as a safety net for
  transcripts left by older versions.

`logging.dir` may point to any folder with any permissions (a data drive readable by all users); the agent does not
restrict it, so it does not guarantee that only administrators read the transcript. Parsing it would also need more of
the YAML than the scripts read today. Always TEMP is private but leaves the file where nobody looks for node logs. Keeping
the script folder and relying on `.gitignore` alone still writes identifying content into the working tree, where any
other tool, archive or a clone without the rule picks it up.

### Consequences

* Good, because a run from a checkout no longer writes anything into the working tree.
* Good, because the transcript sits with the agent's own logs and is readable for administrators only.
* Bad, because the transcript is not next to the script any more; the `Log:` line and the documentation say where it is.
* Neutral, because a child whose `env` or `args` names a path inside `logs` would get access to that folder through
  ADR-0088's grants; that is an operator's configuration choice, and the transcript holds no keys or passwords.

### Confirmation

`test/selftest_ollama_loopback.ps1` (pwsh on Linux, Windows PowerShell 5.1 on Windows in CI) checks how the folder is
derived from service command lines, that `Set-OllamaLoopback.ps1` has no log path built from its own folder and only one
`Start-Transcript`, the one in `Start-RunTranscript`, and on Windows writes a real transcript and checks that it lies
outside the checkout. `test/selftest_ollama_protected.ps1` checks the same for `Move-OllamaProtected.ps1`. Against the
scripts before this change both tests fail.
