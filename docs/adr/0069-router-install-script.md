---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Router install script for Debian and Ubuntu

> Source: documentation review of the first installation. Complements ADR-0044 (hardened unit) and ADR-0067 (`--init`).

## Context and Problem Statement

Installing the router by hand meant copying files, creating a Python venv the unit expects (the README did not mention
it; only the maintainer's `deploy.py` created it), installing units and checking that the configuration's files are
readable by root without capabilities (ADR-0044). A missed step showed up only as a service that does not start.

## Decision Drivers

* One command for install and upgrade on the common target (Debian/Ubuntu), with the same checks the maintainer's deploy
  runs.
* Check before acting; refuse instead of half-installing; never touch the operator's configuration.
* Testable in CI without root.

## Considered Options

* `router/install.sh`: checks, packages via apt, program files, venv with the full requirements, units, configuration
  check like the service; staging mode `--root DIR --no-venv` for packaging and tests
* A Debian package
* A container image

## Decision Outcome

Chosen option: "Install script", because it serves the documented target with the least machinery and stays readable. A
Debian package needs a build and repository; a container image changes the hardening model of ADR-0044 and would need
its own decision.

* Before any change: root (unless `--root`), Debian or Ubuntu (`/etc/os-release`), Python >= 3.11, a complete source
  folder. Missing apt packages are installed (or listed with `--check`).
* Program files are staged and swapped; the configuration folder is created 0750 and never written; units are copied
  and systemd reloaded. The venv uses system site packages plus `requirements-session.txt`, which pins `paho-mqtt<2` even
  where the distribution ships 2.x.
* With a configuration: `router.py --check --files` under `setpriv` without capabilities, then `--posture`; a failing
  check refuses with the program files installed and the service untouched. Without one: the next step is `--init`.
* Last line `STATE: installed | checked | refused`.

### Consequences

* Good, because install and upgrade are one command with the same checks as the maintainer's deploy.
* Good, because the script is exercised in CI.
* Bad, because apt, systemctl and the venv are only exercised on a real machine, not in CI's staging mode.
* Neutral, because other distributions follow the manual steps.

### Confirmation

`test/selftest_install.py` (Linux): layout, configuration folder 0750, upgrade without leftovers, `--check`, refusals
(other distribution, unknown option, not root), a valid configuration from `--init` passes and prints the posture, a
broken one is refused while the program files stay.
