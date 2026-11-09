#!/usr/bin/env python3
"""Self-test (ADR-0067): `router.py --init` writes a first configuration that is valid and safe, and refuses otherwise.

Runs the real CLI as a process with the password on stdin (no terminal): the file is created owner-only, loads like
--check, has no high or medium posture finding, stores the password only as a PBKDF2 hash and the client token only as
sha256, and prints the token once. Refusals: an existing file stays untouched, a short password, a bad client name, a
URL that is not https. Also checks that config.minimal.yaml itself loads and that its placeholders are reported by the
posture check. Synthetic values only."""
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTER = os.path.join(HERE, "..", "router", "router.py")
MINIMAL = os.path.join(HERE, "..", "router", "config.minimal.yaml")
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, config, posture  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

PASSWORD = "synthetic-password-1234"


def run(*args, stdin=PASSWORD + "\n"):
    return subprocess.run([sys.executable, ROUTER, *args], input=stdin, capture_output=True, text=True, encoding="utf-8",
                          env={**os.environ, "PYTHONUTF8": "1"})


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = os.path.join(self.tmp.name, "etc")
        self.path = os.path.join(self.target, "config.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_a_valid_safe_configuration(self):
        r = run("--init", self.target, "--public-url", "https://router.example.invalid:11435", "--client", "example-client")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "INIT: written")
        token = re.search(r"token, shown only now.*\n  (\S+)\n", r.stdout).group(1)
        text = open(self.path, encoding="utf-8").read()
        self.assertNotIn(PASSWORD, text, "the password is stored only as a hash")
        self.assertNotIn(token, text, "the token is stored only as sha256")
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), text)
        self.assertEqual(r.stdout.count(token), 1, "the token is shown once")
        self.assertIn("https://router.example.invalid:11435", text)
        if hasattr(os, "fchmod"):
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        cfg = config.Config.load(self.path, use_overrides=False)
        self.assertFalse(failed(cfg), getattr(cfg, "message", cfg))
        self.assertTrue(auth.verify_password(PASSWORD, cfg.control_users["admin"]))
        self.assertEqual(cfg.client_auth["mode"], "enforce")
        severities = {f.severity for f in posture.findings(cfg, {})}
        self.assertFalse(severities & {"high", "medium"}, posture.report(cfg, {})[0])

    def test_refusals_change_nothing(self):
        self.assertEqual(run("--init", self.target).returncode, 0)
        before = open(self.path, encoding="utf-8").read()
        r = run("--init", self.target)
        self.assertEqual((r.returncode, r.stdout.strip().splitlines()[-1]), (2, "INIT: refused"))
        self.assertEqual(open(self.path, encoding="utf-8").read(), before, "an existing configuration stays untouched")
        other = os.path.join(self.tmp.name, "other")
        for args, stdin in ((("--init", other), "short\n"), (("--init", other, "--client", "bad name"), PASSWORD + "\n"),
                            (("--init", other, "--public-url", "http://router.example.invalid"), PASSWORD + "\n")):
            with self.subTest(args=args):
                r = run(*args, stdin=stdin)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertFalse(os.path.exists(os.path.join(other, "config.yaml")))

    def test_minimal_template_loads_and_its_placeholders_are_reported(self):
        cfg = config.Config.load(MINIMAL, use_overrides=False)
        self.assertFalse(failed(cfg), getattr(cfg, "message", cfg))
        codes = {f.code for f in posture.findings(cfg, {})}
        self.assertTrue({"client_example_token", "control_without_users"} <= codes, codes)

    def test_help_and_unknown_option(self):
        self.assertIn("--init", run("--help").stdout)
        r = run("--no-such-option")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
