---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# Linux agent unit without writes to shared program folders

> Source: TM-14 follow-up after ADR-0073 (risk register: "Linux agent unit without /usr/local/bin write access").

## Context and Problem Statement

The Linux agent runs as root. Its unit (ADR-0070) used `ProtectSystem=full` and allowed writes to
`/var/lib/skirnir-agent`, `/var/log/skirnir-agent` and `/usr/local/bin`, because the self-update (`updater.go`) places
the new binary next to the running one and swaps it. `/usr/local/bin` comes before `/usr/bin` in every default `PATH`:
a compromised agent could plant `apt`, `sudo` or `ls` there and wait for root or a user to run them. `ProtectSystem=full`
also left `/var`, `/home` and `/root` writable, and the process kept every capability, including `CAP_SYS_ADMIN`, with
which it could remount the read-only parts writable again.

The agent needs few privileges: network, its own state and log folders, its binary for the self-update, the NVIDIA tools
for measurements, and `nvidia-smi -pl` for the GPU guard. The NVIDIA driver allows setting the power limit only to a
process with `CAP_SYS_ADMIN`.

## Decision Drivers

* The self-update keeps working without the service writing any folder other programs are run from.
* The GPU guard keeps setting the power limit (default on; ADR of the guard, design/gpu-guard.md).
* A remaining capability must not undo the file system restrictions.
* Existing nodes move to the new layout by running the tool they already know (`setup`), not by hand.

## Considered Options

* Own program folder `/opt/skirnir-agent` (writable for the service, not on `PATH`), a link in `/usr/local/bin`,
  `ProtectSystem=strict`, `CAP_SYS_ADMIN` as the only capability, a system call filter without mount calls
* The same without any capability (no power limit on Linux)
* A separate root helper for the self-update and the power limit, the agent itself unprivileged
* Keep the unit

## Decision Outcome

Chosen option: "own program folder, strict unit, `CAP_SYS_ADMIN` behind a system call filter", because it removes the
`PATH` planting and the writable system at once, keeps both features, and needs no second process.

* The binary is `/opt/skirnir-agent/skirnir-agent`; `/usr/local/bin/skirnir-agent` is a symlink to it for the command
  line. `ExecStart` uses the real path, so the self-update swaps the file in `/opt/skirnir-agent`.
* Unit: `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=/var/lib/skirnir-agent /var/log/skirnir-agent
  /opt/skirnir-agent -/root/.ollama` (the last one only for an Ollama child's models), `PrivateTmp`,
  `CapabilityBoundingSet=CAP_SYS_ADMIN`, `SystemCallFilter=@system-service` with `SystemCallErrorNumber=EPERM`,
  `RestrictNamespaces`, `RestrictRealtime`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`,
  `ProtectKernelLogs`, `ProtectClock`, `ProtectHostname`, `UMask=0077`, plus the existing protections.
* `skirnir-agent setup` installs this layout. On a node set up before, it copies the binary to `/opt`, replaces the old
  binary in `/usr/local/bin` by the link in one rename, writes the unit and restarts the service (`restart` instead of
  `enable --now`, which would leave a running service on the old unit).

### Consequences

* Good, because the service can no longer place programs on anyone's `PATH` or change the system, `/etc`, homes or
  `/var` outside its folders.
* Good, because `CAP_SYS_ADMIN` cannot remount: the filter removes the mount calls (shown below).
* Bad, because `CAP_SYS_ADMIN` remains broad within the allowed system calls; a separate helper (third option) would
  remove it from the agent and stays possible later.
* Bad, because an Ollama child that keeps its models elsewhere than `/root/.ollama` needs a `ReadWritePaths=` drop-in.
* Bad, because a Linux node keeps the old unit until `setup` runs again; the self-update replaces only the binary.

### Confirmation

* `agent/setup_linux_test.go`: a staged setup installs the binary in `/opt`, the link, and a unit with
  `ProtectSystem=strict`, `CAP_SYS_ADMIN` and the filter; an old installation (binary and unit allowing
  `/usr/local/bin`) is moved, and a second run keeps the link.
* Manual check on a systemd host (WSL Ubuntu, 2026-11-12): old layout, then `setup`; the service ran from
  `/opt/skirnir-agent`, measured the GPU through `nvidia-smi`, created its key in `/var/lib/skirnir-agent`; inside its
  namespace `/usr/local/bin`, `/etc/skirnir-agent` and `/root` were read-only and only its three folders writable;
  `CapBnd` was `CAP_SYS_ADMIN` alone, seccomp active; `systemd-analyze security` 2.7. A transient unit with the same
  restrictions could not remount `/` writable (`permission denied`); the control run without the filter could, and
  then wrote to `/etc`.

## Pros and Cons of the Options

### No capability at all

* Good, because the narrowest unit.
* Bad, because the GPU guard could no longer lower the power limit on Linux - the guard exists to protect the hardware.

### Separate privileged helper

* Good, because the agent itself could run without root.
* Bad, because a second process, an interface between both and its own update path; more than this risk needs now.

### Keep the unit

* Bad, because the `PATH` planting and the writable system stay.

## More Information

Threat model TM-14. Builds on ADR-0070 (Linux setup). The Windows side of TM-14 for children is ADR-0085.
