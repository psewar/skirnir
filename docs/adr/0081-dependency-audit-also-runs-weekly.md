---
status: accepted
date: 2026-11-12
decision-makers: maintainer
---

# The dependency audit also runs weekly

> Source: repository review of 2026-11-12; refines ADR-0045 and makes the weekly `pip-audit` that ADR-0074 relies on real.

## Context and Problem Statement

ADR-0045 put `pip-audit` and `govulncheck` into `.github/workflows/security.yml` so that vulnerable dependencies show up
"without manual checks". The workflow ran on pushes to `main`, on pull requests and by hand, but not on a schedule.
ADR-0074 then hash-locked the Python installs and named the cost: Dependabot does not refresh these locks, "a CVE shows in
the weekly `pip-audit`". There was no weekly run, and Dependabot alerts are not enabled for the repository.

An advisory published after the last push for a locked Python package or a Go module therefore surfaced nowhere until the
next commit. That gap is widest exactly when the project is quiet and nobody looks.

## Decision Drivers

* A newly published vulnerability in a locked or required dependency surfaces without anyone pushing or looking.
* The control lives in the repository, where a pull request can review it and CI can check that it stays.
* No new permissions, tokens or accounts; the workflow keeps `contents: read`.
* Few runs and little noise for a small project.

## Considered Options

* A weekly `schedule` trigger on the existing security workflow
* A daily `schedule` trigger
* Enable Dependabot alerts in the repository settings instead
* Keep auditing only on pushes and pull requests

## Decision Outcome

Chosen option: "a weekly `schedule` trigger on the existing security workflow", because it is the cadence ADR-0074
already assumes, needs no new moving part and is checked in CI.

* `security.yml` runs every Monday at 04:29 UTC (`29 4 * * 1`), before the weekly threat-model run, with the same jobs as
  on a push: `pip-audit` over the runtime, optional and both hash-locked requirement sets, `lock_requirements.py
  verify-pypi`, and `govulncheck ./...` for the agent. Push, pull request and manual dispatch stay; a manual run is the
  way to audit at once.
* `test/selftest_dependency_audit.py` (in CI) fails if the schedule disappears or runs less often than weekly, if the push
  or pull request trigger is dropped, if a job or one of these audit steps is removed or made conditional.

A daily run would report a few days earlier, at seven times the runs against PyPI, the vulnerability databases and the
runners; for this project a week is an acceptable delay, and a manual run is available. Dependabot alerts are a repository
setting: a pull request cannot review them and CI cannot see whether they are on, and they do not do `govulncheck`'s
check whether the vulnerable code is reachable. The maintainer may still enable them in addition; that is a setting, not
part of this decision. Auditing only on pushes is the gap this record closes.

### Consequences

* Good, because a new advisory for a dependency of the router, the CI or the agent turns a scheduled run red within a week,
  without a commit. GitHub sends the failure notice of a scheduled run to the account that last changed the `cron` line.
* Good, because removing the schedule or an audit step is now a failing check, not a silent regression.
* Bad, because the scheduled run can turn red without a code change (as ADR-0045 already accepted for pushes); the finding
  is then fixed by a lock refresh or update in a pull request, or accepted explicitly.
* Bad, because GitHub disables scheduled workflows of a public repository after 60 days without repository activity; the
  maintainer then re-enables the workflow in the Actions tab.
* Neutral, because the schedule only detects. Refreshing the Python locks stays a manual `pin`/`resolve` in a pull request
  (TM-16).

### Confirmation

`test/selftest_dependency_audit.py` runs in CI. It parses the shipped `security.yml` and accepts it; variants without the
schedule, with a monthly schedule, with a condition that skips the Go job on the scheduled run, without the audit of the
router lock, without `govulncheck`, with the lock audits limited to pushes, or without the pull request trigger are each
rejected; a daily schedule is accepted. Run against the workflow before this change, the test fails.
