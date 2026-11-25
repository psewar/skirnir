---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude while working through the open risk-register items)
---

# The Python locks are checked weekly against a fresh resolution

> Threat: TM-16 (next action "Refresh the Python locks regularly (no Dependabot support for them)").
> Builds on [ADR-0074](0074-hash-locked-python-installs.md) (hash-locked Python installs),
> [ADR-0081](0081-dependency-audit-also-runs-weekly.md) (the dependency audit also runs weekly) and
> [ADR-0121](0121-the-router-image-installs-its-own-complete-hash-lock-on-the-official-python-image.md) (the container
> lock follows the CI lock).

## Context and Problem Statement

The router hosts, the container image and CI install Python packages only from hash locks (ADR-0074). Dependabot does
not maintain these locks: they list every hash PyPI publishes, across platforms. They change only when someone runs
`lock_requirements.py` by hand.

The weekly `pip-audit` (ADR-0081) catches a locked version with a known vulnerability. It does not catch a lock that
simply falls behind. Bug fixes and hardening that never get a CVE stay out, and when a vulnerability does appear, the
jump to a fixed version is large.

The risk register calls this out as V14: "lock freshness depends on a manual refresh". A rule that depends on
remembering is the weakest protection (AGENTS.md, "Tools check their own preconditions").

## Decision Drivers

* Say when a refresh is due, without false alarms from version bounds, Python markers or platform wheels.
* Never turn an unrelated pull request red because upstream published something.
* Give a fresh release time to settle (yanks, regressions) before it is demanded.
* Holding a version back is explicit and has a stated reason, not a silent switch.
* No write token and no unattended commits in CI (TM-16 itself).

## Considered Options

* **Chosen: `lock_requirements.py outdated`, run weekly and on demand.** It lets pip resolve the CI requirements the
  same way `resolve` does. It lists every lock entry whose package would get another version, and every package the CI
  lock would drop or add. It fails when such a version is older than 30 days.
* **Report only, never fail.** Rejected: a green job's log is read by no one, and the gap stays a manual habit.
* **Fail on any newer release.** Rejected: the run would be red most weeks, and a release gets no time to be yanked or
  fixed. Known vulnerabilities need no grace; `pip-audit` reports them independently.
* **A bot that opens refresh pull requests.** Rejected:
  * it needs a write token in a scheduled job;
  * pull requests opened with `GITHUB_TOKEN` start no CI;
  * a refresh pull request needs a security review record that a bot cannot honestly write (AGENTS.md, ADR-0050).
* **Dependabot for pip.** Rejected in ADR-0074: it does not maintain all-platform hash locks.
* **Run on pull requests too.** Rejected: an upstream release would block unrelated work.

## Decision Outcome

Chosen option: "`outdated`, weekly and on demand, failing after a 30-day grace". It turns "refresh regularly" into a
check with a threshold, and it asks pip itself, so version bounds and markers cannot cause false alarms.

**How the check works**

* `pip_resolve` is the resolution `resolve` already used, now shared by both commands: Linux x86_64, CPython 3.11,
  wheels only.
* `outdated` compares each entry of the router, container and CI locks with that resolution.
* A different version is a pending change. Its age counts from the first upload of that version on PyPI.
* A change becomes **overdue** when the version is older than `--grace-days` (default 30).
* An unknown release date is listed but not overdue. So are CI-lock packages that would be dropped or added: they come
  with another package's version change.
* The command prints every change, and fails only for overdue ones. The failure names the refresh commands and the
  explicit way out.

**Holding a version back.** Put a bound in its requirements file, with a comment that says why. pip then respects the
bound, and the check stays green.

**Where it runs.** `security.yml` gets the job `python-lock-freshness`. It runs only on the weekly schedule and on
`workflow_dispatch`, on CPython 3.11 like CI, and installs nothing: the tool uses only the standard library and pip.

### Consequences

* Good, because a lock that falls behind becomes a red weekly run with the exact packages and commands. The gap is no
  longer a matter of memory.
* Good, because pip's own resolution means no false alarm from bounds, markers or wheels the CI target cannot use.
* Bad, because entries that only the router or container lock has (packages pip does not need for the CI target) are
  not compared. Their versions still follow the CI lock through the existing checks (ADR-0121).
* Bad, because the check needs PyPI, so a PyPI outage turns the weekly run red. That is the same as `verify-pypi`.
* Neutral: the refresh itself stays a maintainer pull request, with its security review.

### Confirmation

`test/selftest_lock_freshness.py` runs in CI without network.

**Comparison:**

* Nothing is pending when pip picks the locked versions; names are compared in canonical form.
* A newer release is overdue only past the grace: exactly 30 days is not overdue.
* The router lock is compared with the same resolution.
* An unknown release date is listed, not overdue.
* Dropped and added CI packages are listed, never overdue.

**Command:**

* Passes on the current locks.
* Fails for an overdue release and names the explicit bound.
* Passes for a release inside the grace.
* Refuses a negative grace before any resolution.
* Refuses a platform other than Linux.

**Workflow:**

* The job runs only on schedule and dispatch, never on push or pull request.
* It uses the default grace.

A live run against PyPI on 2026-11-25 found 0 pending changes: pip resolved 31 packages, all equal to the CI lock. A
probe with one lowered entry was reported as overdue with its real release date.
