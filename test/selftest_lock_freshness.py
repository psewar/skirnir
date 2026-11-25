#!/usr/bin/env python3
"""Self-test (TM-16, ADR-0144): a weekly check says when the hash locks are overdue for a refresh.

lock_requirements.pending_changes compares every lock entry with what pip resolves today for the CI requirements:
a different version is a pending change, overdue once that version is older than the grace (30 days); an unknown
release date is listed, not overdue; for the CI lock, packages pip no longer or newly needs are listed, never overdue.
`outdated` prints the changes and fails only for overdue ones; a negative grace is refused before any resolution. The
security workflow runs it weekly and on demand, never on pushes or pull requests. Runs without network: pip and PyPI
are replaced by synthetic values."""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools", "security"))
import lock_requirements as lr  # noqa: E402

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "security.yml")


def entry(name, version):
    return {"name": name, "version": version, "hashes": ["0" * 64]}


class PendingChangeTests(unittest.TestCase):
    def setUp(self):
        self.dates = {}

    def released_at(self, name, version):
        return self.dates.get((name, version))

    def changes(self, locks, resolved, grace=30):
        return lr.pending_changes(locks, resolved, self.released_at, NOW, grace)

    def test_nothing_is_pending_when_pip_picks_the_locked_versions(self):
        locks = {lr.CI_LOCK: {"aiohttp": entry("aiohttp", "3.14.5"), "typing-extensions": entry("typing_extensions", "4.15.0")}}
        self.assertEqual(self.changes(locks, [("aiohttp", "3.14.5"), ("typing_extensions", "4.15.0")]), [],
                         "names are compared in canonical form")

    def test_a_newer_release_is_overdue_only_after_the_grace(self):
        locks = {lr.CI_LOCK: {"aiohttp": entry("aiohttp", "3.14.4"), "yarl": entry("yarl", "1.25.0"),
                              "multidict": entry("multidict", "6.0.0")},
                 lr.ROUTER_LOCK: {"aiohttp": entry("aiohttp", "3.14.4")}}
        self.dates = {("aiohttp", "3.14.5"): NOW - timedelta(days=45), ("yarl", "1.25.1"): NOW - timedelta(days=30)}
        found = {(c["lock"], c["name"]): c for c in
                 self.changes(locks, [("aiohttp", "3.14.5"), ("yarl", "1.25.1"), ("multidict", "6.1.0")])}
        self.assertEqual(found[(lr.CI_LOCK, "aiohttp")],
                         {"lock": lr.CI_LOCK, "name": "aiohttp", "locked": "3.14.4", "fresh": "3.14.5", "age_days": 45,
                          "overdue": True})
        self.assertTrue(found[(lr.ROUTER_LOCK, "aiohttp")]["overdue"], "the router lock is compared with the same resolution")
        self.assertFalse(found[(lr.CI_LOCK, "yarl")]["overdue"], "exactly the grace is not overdue yet")
        self.assertEqual((found[(lr.CI_LOCK, "multidict")]["age_days"], found[(lr.CI_LOCK, "multidict")]["overdue"]),
                         (None, False), "an unknown release date is listed, not overdue")
        self.assertEqual(lr.describe(found[(lr.CI_LOCK, "aiohttp")]),
                         "requirements-ci-lock.txt: aiohttp 3.14.4 -> 3.14.5 (released 45 days ago, OVERDUE)")
        self.assertEqual(lr.describe(found[(lr.CI_LOCK, "multidict")]),
                         "requirements-ci-lock.txt: multidict 6.0.0 -> 6.1.0 (release date unknown)")
        self.assertFalse(any(c["overdue"] for c in self.changes(locks, [("aiohttp", "3.14.5")], grace=60)),
                         "a longer grace keeps the same release pending, not overdue")

    def test_packages_the_ci_lock_would_drop_or_add(self):
        locks = {lr.CI_LOCK: {"old-dep": entry("old-dep", "1.0")}, lr.ROUTER_LOCK: {"router-only": entry("router-only", "2.0")}}
        changes = self.changes(locks, [("New_Dep", "0.3")])
        self.assertEqual([lr.describe(c) for c in changes],
                         ["requirements-ci-lock.txt: old-dep 1.0 is no longer needed",
                          "requirements-ci-lock.txt: New_Dep 0.3 is newly needed"])
        self.assertFalse(any(c["overdue"] for c in changes), "they come with another package's version change")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.real = lr.pip_resolve, lr.released

    def tearDown(self):
        lr.pip_resolve, lr.released = self.real

    def outdated(self, resolved, dates, *args):
        lr.pip_resolve = lambda requirements: resolved
        lr.released = lambda name, version: dates.get((name, version))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = lr.main(["outdated", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_current_locks_pass_and_an_overdue_release_fails(self):
        ci, _ = lr.load(lr.CI_LOCK)
        current = [(e["name"], e["version"]) for e in ci.values()]
        rc, out, _ = self.outdated(current, {})
        self.assertEqual(rc, 0, out)
        self.assertIn("Lock freshness: 0 pending change(s), 0 older than 30 days", out)
        name = ci["aiohttp"]["name"]
        newer = [(n, "99.0") if n == name else (n, v) for n, v in current]
        rc, out, err = self.outdated(newer, {(name, "99.0"): NOW.replace(year=2020)})
        self.assertEqual(rc, 1)
        self.assertIn(f"{name} {ci['aiohttp']['version']} -> 99.0", out)
        self.assertIn("OVERDUE", out)
        self.assertIn("overdue for a refresh", err)
        self.assertIn("bound the version in its requirements file", err, "the explicit way to hold a version back")
        rc, out, _ = self.outdated(newer, {(name, "99.0"): datetime.now(timezone.utc) - timedelta(days=2)})
        self.assertEqual(rc, 0, "a release inside the grace is listed but does not fail")
        self.assertIn("-> 99.0 (released 2 days ago)", out)

    def test_a_negative_grace_is_refused_before_any_resolution(self):
        def must_not_run(requirements):
            raise AssertionError("pip resolution started")
        lr.pip_resolve = must_not_run
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lr.main(["outdated", "--grace-days", "-1"]), 1)
        self.assertIn("--grace-days must be 0 or more", err.getvalue())

    @unittest.skipIf(sys.platform.startswith("linux"), "on Linux pip_resolve resolves for real (network)")
    def test_pip_resolution_refuses_other_platforms(self):
        self.assertIn("runs on Linux only", lr.pip_resolve(list(lr.CI_REQS)))


class WorkflowTests(unittest.TestCase):
    def test_the_check_runs_weekly_and_on_demand_never_on_pull_requests(self):
        with open(WORKFLOW, encoding="utf-8") as f:
            workflow = yaml.safe_load(f)
        on = workflow.get("on", workflow.get(True))
        self.assertIn("schedule", on)
        job = workflow["jobs"]["python-lock-freshness"]
        condition = job.get("if", "")
        for event in ("schedule", "workflow_dispatch"):
            self.assertIn(f"github.event_name == '{event}'", condition)
        for event in ("pull_request", "push"):
            self.assertNotIn(event, condition, "an upstream release must not turn an unrelated pull request red")
        runs = [str(step.get("run", "")) for step in job["steps"]]
        self.assertTrue(any("lock_requirements.py outdated" in r for r in runs))
        self.assertFalse(any("--grace-days" in r for r in runs), "the grace is the tool's default (ADR-0144)")


if __name__ == "__main__":
    unittest.main(verbosity=1)
