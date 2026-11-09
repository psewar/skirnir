---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Windows node script that binds Ollama to loopback

> Source: TM-11 inspection on 2026-11-09 - Ollama on both Windows nodes listened on all addresses; only the host
> firewall kept the router host out. The operator asked for one script to run per node.

## Context and Problem Statement

The agent reaches Ollama on `127.0.0.1:11434`; the router reaches it only through the agent's tunnel. An Ollama that
listens on all addresses is reachable around the router (no client authentication, no privacy check, no journal) by
anything the host firewall lets through. The documentation says to set `OLLAMA_HOST=127.0.0.1`, but where to set it
differs per node: in the env of the agent's Ollama child (a machine-wide variable reaches a service only after a reboot,
a user variable never), or for the signed-in user who runs the Ollama desktop app. Changing it by hand means editing a
configuration that only administrators can read, restarting the right process in the right session, and checking the
result - on a machine of another household member, through a PowerShell that blocks scripts by default.

## Decision Drivers

* One command per node, started by double-click despite the default execution policy, asking for administrator rights
  itself.
* The script finds out how Ollama runs; the operator does not have to know or say it.
* Check before acting: nothing changes if the agent itself would lose Ollama or if other machines use Ollama right now.
* Verify the result, and put the previous state back when the verification fails.

## Considered Options

* `agent/Set-OllamaLoopback.ps1` with a `.cmd` launcher (like `Set-UpdateKey`, ADR-0066)
* A new `skirnir-agent` subcommand
* Documentation only

## Decision Outcome

Chosen option: "`Set-OllamaLoopback.ps1` with a `.cmd` launcher", because the change also concerns an Ollama the agent
does not run (desktop app in a user's session) and works on nodes with any agent version; a subcommand would need a new
agent release first and would reach into another user's session from a service. Documentation alone left the nodes as
they were.

* Detects the mode: an agent child running `ollama.exe` (or named `ollama*`) - its env gets `OLLAMA_HOST=127.0.0.1:11434`
  (replaced, or added so a machine-wide value cannot reach the child), `check-config` with the installed agent, service
  restart; or the desktop app / `ollama serve` of a signed-in user - that user's `OLLAMA_HOST` (and the machine-wide
  one, if it names a network address) becomes the loopback value and the app is restarted in that user's session through
  a one-shot scheduled task.
* Refuses: no administrator rights after the UAC prompt, more than one Ollama child, an inline or duplicated env entry,
  an agent setting that reaches port 11434 over a network address, another program on a network address while the child
  is already set, other machines connected to port 11434 right now (`-Force "<reason>"` continues and logs the reason).
* Verifies that Ollama answers on `127.0.0.1` and that every listener on the port is a loopback address; otherwise the
  configuration backup or the previous variables are restored.
* Then disables (does not delete) enabled inbound allow rules that only opened Ollama - port 11434 alone or the Ollama
  program - and prints the command to enable each again. Rules that open further ports stay.
* `-Check` only reports. A log is written next to the script; the last line is `STATE: changed | current | checked |
  refused | restored`.

### Consequences

* Good, because each node is one double-click, and the outcome is verified rather than assumed.
* Good, because the firewall rules that only existed for direct Ollama access are closed in the same step.
* Bad, because the restart interrupts requests on that node (the router retries elsewhere) and unloads its models.
* Bad, because programs that used Ollama directly over the network stop working; they must use the router. The script
  shows current connections, not ones that happen later.
* Neutral, because Linux nodes keep the documented `systemctl edit ollama` step.

### Confirmation

`test/selftest_ollama_loopback.ps1` (Windows PowerShell 5.1 in the Windows job, pwsh on Linux in CI): value parsing for
loopback and network forms; the Ollama child found by name or program; only its `OLLAMA_HOST` line changes (quotes,
comments, CRLF and other children unchanged), added to a list, a mapping or a missing env; refusals for no or two Ollama
children, inline env and duplicates; network upstreams on port 11434 found, loopback ones and other ports not.
