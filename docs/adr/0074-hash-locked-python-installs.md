---
status: accepted
date: 2026-11-10
decision-makers: operator
---

# Hash-locked Python installs on top of distribution packages

> Source: TM-16 next action "hash-locked Python installs"; inspection of the router host on 2026-11-09.

## Context and Problem Statement

The router's venv uses the distribution's packages (`--system-site-packages`): on Debian 12 aiohttp, PyYAML,
cryptography, paho-mqtt and uvloop come from apt, signed by the distribution. pip then installed whatever
`requirements-session.txt` still needed by range - in production `asyncpg>=0.29,<1` - so each deploy could fetch a
different, unverified release from PyPI. CI installed every requirement file by range as well, so the tested versions
were whatever PyPI served that day. Dependabot covers Go, actions and images, but not these files.

## Decision Drivers

* No unpinned or unhashed artifact from PyPI on a router host, and the same for CI.
* Keep the distribution's signed, security-maintained packages where they meet the requirements.
* One lock for every supported host (Debian 12/13, Ubuntu 24.04; Python 3.11 to 3.14).
* Refreshing a lock is one command; a stale or wrong lock fails CI.

## Considered Options

* A hash-locked overlay for what the distribution lacks, planned per host, plus a hashed CI lock
* A full venv from PyPI with a hashed lock (no distribution packages)
* pip-tools or uv lock files

## Decision Outcome

Chosen option: "hash-locked overlay plus CI lock", because it keeps the distribution's packages and still makes every
pip artifact fixed and verified. A full PyPI venv would drop the distribution's security updates for the core packages;
pip-tools and uv would add a tool the hosts do not have, and their single-platform locks do not fit the mix of
distributions.

* `router/requirements-lock.txt` names exact versions with every SHA-256 PyPI publishes for them (all wheels and the
  sdist, so one lock serves every platform). `skirnir_router/pydeps.py` (standard library plus `packaging` or pip's
  vendored copy) lists the requirements the running interpreter does not meet, and lock entries whose venv copy differs;
  pip installs exactly those with `--require-hashes --no-deps`, then `pydeps.py verify` checks every requirement. An unmet
  requirement without a lock entry, a locked version outside the requirement, or an entry without hash stops the
  install. `install.sh` does this; `deploy.py` sends `pydeps.py` and the requirement files to the host before the swap.
* `install.sh` installs `python3-paho-mqtt` and `python3-uvloop` from apt as well. The router asks paho-mqtt 2.x for the
  1.x callback API, so the requirement is `paho-mqtt>=1.6.1` and Debian 13's 2.x package serves.
* `requirements-ci-lock.txt` holds everything CI installs (Linux, CPython 3.11), resolved by pip and hashed from PyPI;
  every workflow installs it with `--require-hashes`.
* `tools/security/lock_requirements.py`: `pin` (router lock; refuses when a pinned package needs an unpinned dependency on
  a supported Python), `resolve` (CI lock, Linux only - pip evaluates markers for the running system), `check` (offline,
  in CI), `verify-pypi` (in the security workflow: every hash is one PyPI publishes, no locked version yanked).
  `pip-audit` audits both locks as written.

### Consequences

* Good, because what pip installs on a host or in CI is fixed and verified, and a tampered or mistyped hash fails.
* Good, because the core packages stay with the distribution's security updates.
* Bad, because Dependabot does not refresh these locks; a CVE shows in the weekly `pip-audit`, and a refresh is a manual
  `pin`/`resolve` in a PR.
* Bad, because the router's dependency versions now differ by distribution for the core packages, as before; only the
  pip overlay is fixed.

### Confirmation

`test/selftest_pydeps.py`: synthetic hosts (Debian 12 set, venv with the locked or another version, missing packages)
get exactly the expected plan; refusals for unmet-and-unlocked, out-of-range, unhashed and malformed entries; markers and
`-r` includes; the shipped lock on Debian 12 packages installs only `asyncpg`. `lock_requirements.py check` in CI;
`verify-pypi` and `pip-audit --require-hashes` in the security workflow.
