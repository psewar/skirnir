#!/usr/bin/env python3
"""Self-test (TM-15, ADR-0132): the audit log rotates by size when the operator sets a limit.

client_auth.audit_log_max_mib (0 or absent = no rotation, as before) and audit_log_keep (default 5): before a line is
written to a file that reached the limit, the file becomes <path>.1, older generations move up, and the one beyond
<path>.<keep> is removed. New and rotated files stay owner-only (POSIX). The configuration refuses values outside the
ranges. Synthetic events only; temporary folder; no network."""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, config, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

MIB = 1024 * 1024


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "logs", "audit.jsonl")

    def tearDown(self):
        state.CFG = self.previous
        self.tmp.cleanup()

    def configure(self, **extra):
        state.CFG = SimpleNamespace(client_auth={"mode": "enforce", "clients": {}, "audit_log": self.path, **extra})

    def fill(self, path, marker):
        """A file of exactly 1 MiB whose first line names it."""
        head = json.dumps({"event": "synthetic", "marker": marker}) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + "x" * (MIB - len(head) - 1) + "\n")

    def first_marker(self, path):
        with open(path, encoding="utf-8") as f:
            return json.loads(f.readline()).get("marker")

    def test_without_a_limit_the_log_only_grows(self):
        self.configure()
        auth.audit("synthetic_event", client="synthetic-client")
        self.fill(self.path, "big")
        auth.audit("synthetic_event", client="synthetic-client")
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.path))), ["audit.jsonl"], "no rotation by default")
        self.assertGreater(os.path.getsize(self.path), MIB)

    def test_a_full_log_rotates_and_keeps_the_newest_generations(self):
        self.configure(audit_log_max_mib=1, audit_log_keep=3)
        auth.audit("synthetic_event", n=0)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["audit.jsonl"], "below the limit nothing rotates")
        for n in range(1, 6):
            self.fill(self.path, f"generation-{n}")
            auth.audit("synthetic_event", n=n)
            with open(self.path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual([json.loads(x)["n"] for x in lines], [n], "the line after a rotation starts a new file")
        names = sorted(os.listdir(os.path.dirname(self.path)))
        self.assertEqual(names, ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2", "audit.jsonl.3"], "keep 3, the oldest removed")
        self.assertEqual([self.first_marker(f"{self.path}.{g}") for g in (1, 2, 3)],
                         ["generation-5", "generation-4", "generation-3"], ".1 is the newest rotated generation")

    @unittest.skipIf(os.name != "posix", "file modes are POSIX")
    def test_new_and_rotated_files_stay_owner_only(self):
        self.configure(audit_log_max_mib=1, audit_log_keep=2)
        auth.audit("synthetic_event", n=0)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("x" * MIB + "\n")
        auth.audit("synthetic_event", n=1)
        for p in (self.path, self.path + ".1"):
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, p)

    def test_configuration_takes_only_values_in_range(self):
        def parse(**ca):
            target = SimpleNamespace()
            return target, config.Config._parse_client_auth(target, {"mode": "enforce", "clients": {}, **ca})
        target, problem = parse()
        self.assertIsNone(problem)
        self.assertEqual((target.client_auth["audit_log_max_mib"], target.client_auth["audit_log_keep"]), (0, 5),
                         "absent: no rotation, five generations once a limit is set")
        target, problem = parse(audit_log_max_mib=64, audit_log_keep=10)
        self.assertIsNone(problem)
        self.assertEqual((target.client_auth["audit_log_max_mib"], target.client_auth["audit_log_keep"]), (64, 10))
        for key, value in (("audit_log_max_mib", -1), ("audit_log_max_mib", 4097), ("audit_log_max_mib", "64"),
                           ("audit_log_max_mib", 1.5), ("audit_log_max_mib", True), ("audit_log_keep", 0),
                           ("audit_log_keep", 101), ("audit_log_keep", "5")):
            with self.subTest(key=key, value=value):
                _target, problem = parse(**{key: value})
                self.assertTrue(failed(problem), f"{key}={value!r} is refused")
                self.assertIn(key, problem.message)


if __name__ == "__main__":
    unittest.main(verbosity=1)
