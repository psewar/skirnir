---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The router unit drops what a Python network service does not need

> Threat: TM-14 (next action "fuller unit directive set"). Extends
> [ADR-0086](0086-router-runs-as-its-own-account.md) (own account, read-only configuration).

## Context and Problem Statement

Since ADR-0086 the router runs as its own account without capabilities, with `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome=tmpfs` and `PrivateTmp`, and writes only its state and log folders. A remote code execution in the router
(it parses requests from clients, agents and providers) could still read the kernel log, see every other process in
`/proc`, open raw or packet sockets the router never uses, create namespaces, use real-time scheduling, and call any
system call the kernel offers. `systemd-analyze security` rated the unit 4.9. Directives that remove these cost a
Python network service nothing, but a wrong one stops the router at start, and the unit had never been run under them.

## Decision Drivers

* Take away from a compromised router what the router itself never uses.
* Prove it runs: a directive set is only adopted after the router ran under it with real systemd.
* A site that needs more (another socket family, a device) can open it with a drop-in, without editing the shipped unit.

## Considered Options

* **Add the directives a Python network service needs none of, checked by running the router under the unit in a
  systemd container (`test/unit_hardening_check.sh`).**
* `MemoryDenyWriteExecute=true` as well. Rejected for now: C extensions with callbacks (libffi) may need writable and
  executable memory, which differs between distributions; the gain is small next to the risk of a router that does
  not start after an upgrade.
* A tighter system call list than `@system-service` (`~@privileged ~@resources`). Rejected for now: it needs per-version
  testing of aiohttp, uvloop, asyncpg and the TLS stack; `@system-service` is the documented set for ordinary services.

## Decision Outcome

Chosen option: "the directive set a Python network service needs none of", because it lowers the unit's exposure from
4.9 to 1.5 with directives the router was shown to run under.

* `ProtectKernelLogs`, `ProtectClock`, `ProtectHostname`, `PrivateDevices`, `RestrictNamespaces`, `RestrictRealtime`,
  `RemoveIPC`: no kernel log, clock, hostname, devices beyond the pseudo devices, namespaces, real-time scheduling or
  leftover IPC objects.
* `ProtectProc=invisible`, `ProcSubset=pid`: `/proc` shows only this process; the router reads `/proc/self` only
  (`process_view.py`).
* `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`: IP for clients, nodes, providers and Wake-on-LAN (UDP),
  Unix sockets for local services, netlink for the C library's address lookups.
* `SystemCallFilter=@system-service` with `SystemCallErrorNumber=EPERM`: the system calls of an ordinary service; a call
  outside fails with EPERM instead of killing the process, so a missed case shows as an error, not a crash loop.
* `test/unit_hardening_check.sh` (rootful Podman or Docker on Linux or in WSL; not in CI) builds a throwaway Debian 13
  image with systemd as PID 1, installs the router with `install.sh`, starts it under the unit with TLS and checks the
  API, the client model list, a chat without node, the UI, the admin state, a configuration write into the state folder
  and a reload, and fails on a journal line that looks like a refused system call or file access. AGENTS.md asks for it
  when the unit changes.

### Consequences

* Good, because a compromised router has fewer kernel interfaces, processes, sockets and system calls to work with
  (`systemd-analyze security` 1.5 instead of 4.9).
* Good, because the check that the router runs under the unit is a tool in the repository, not a one-time observation.
* Bad, because the check needs a privileged container and is not part of CI; it ran on Debian 13 (systemd 257), and a
  host with an older systemd ignores directives it does not know (logged at start) rather than refusing them.
* Bad, because a site-specific extension that needs more (another socket family, a device) needs a drop-in.

### Confirmation

`bash test/unit_hardening_check.sh` passed with the new unit (2026-11-25: install, active, API 200, model list 200,
chat 503, UI 200, admin state 200, configuration write 200 with `roles.yaml` written, reload, journal clean, exposure
1.5) and failed with a deliberately too narrow unit (`RestrictAddressFamilies=AF_UNIX`: tracebacks, STATE failed).
`test/selftest_privilege_paths.py` (in CI) checks each directive in the unit file; it fails with the previous unit.
