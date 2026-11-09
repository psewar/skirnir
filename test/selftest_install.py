#!/usr/bin/env python3
"""Self-test (ADR-0069): router/install.sh in its staging mode (--root DIR --no-venv), on Linux.

Checks the file layout (program files, units, configuration folder 0750), a second run as upgrade, --check, the
refusals (other distribution, unknown option, not root without --root), and the configuration step: without one it
names `router.py --init`; with a valid one (written by --init, certificate files present) it passes --check --files and
prints the posture; a broken one is refused while the program files stay installed. apt, systemctl and the venv are not
exercised here (staging mode skips them by design). Synthetic values only."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL = os.path.join(HERE, "..", "router", "install.sh")


@unittest.skipUnless(sys.platform.startswith("linux"), "install.sh targets Debian/Ubuntu; CI runs it on Linux")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        self.os_release = os.path.join(self.tmp.name, "os-release")
        with open(self.os_release, "w", encoding="utf-8") as f:
            f.write("ID=debian\nVERSION_ID=12\n")
        # python3 for the checks is this interpreter, which has the router's packages (as the venv would)
        self.env = {**os.environ, "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")}

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, *extra, os_release=None):
        r = subprocess.run(["bash", INSTALL, "--root", self.root, "--no-venv", "--os-release", os_release or self.os_release, *extra],
                           capture_output=True, text=True, env=self.env)
        return r.returncode, r.stdout, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""

    def test_layout_upgrade_and_check(self):
        rc, out, last = self.install()
        self.assertEqual((rc, last), (0, "STATE: installed"), out)
        prefix = os.path.join(self.root, "opt", "skirnir-router")
        for f in ("router.py", "skirnir_router/app.py", "ui.html", "config.minimal.yaml", "config.example.yaml", "install.sh"):
            self.assertTrue(os.path.exists(os.path.join(prefix, f)), f)
        self.assertFalse(any("__pycache__" in d for d, _, _ in os.walk(prefix)), "no bytecode caches copied")
        self.assertTrue(os.path.exists(os.path.join(self.root, "etc", "systemd", "system", "skirnir-router.service")))
        conf = os.path.join(self.root, "etc", "skirnir-router")
        self.assertEqual(stat.S_IMODE(os.stat(conf).st_mode), 0o750)
        self.assertIn("router.py --init", out, "without a configuration the next step is --init")
        rc, out, last = self.install()
        self.assertEqual((rc, last), (0, "STATE: installed"), "a second run upgrades in place")
        self.assertFalse([n for n in os.listdir(prefix) if n.startswith(".install.") or n.endswith(".old")], "no staging leftovers")
        rc, out, last = self.install("--check")
        self.assertEqual((rc, last), (0, "STATE: checked"), out)

    def test_refusals(self):
        other = os.path.join(self.tmp.name, "fedora")
        with open(other, "w", encoding="utf-8") as f:
            f.write("ID=fedora\n")
        rc, out, last = self.install(os_release=other)
        self.assertEqual((rc, last), (2, "STATE: refused"), out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "opt")), "a refused run changes nothing")
        rc, out, last = self.install("--bogus")
        self.assertEqual((rc, last), (2, "STATE: refused"))
        if os.geteuid() != 0:
            r = subprocess.run(["bash", INSTALL, "--os-release", self.os_release], capture_output=True, text=True, env=self.env)
            self.assertEqual((r.returncode, r.stdout.strip().splitlines()[-1]), (2, "STATE: refused"), "root is required without --root")

    def test_configuration_is_checked_like_the_service_reads_it(self):
        self.assertEqual(self.install()[0], 0)
        conf = os.path.join(self.root, "etc", "skirnir-router")
        cert, key = os.path.join(self.tmp.name, "fullchain.pem"), os.path.join(self.tmp.name, "privkey.pem")
        for path in (cert, key):
            with open(path, "w", encoding="utf-8") as f:
                f.write("synthetic\n")
        init = subprocess.run([sys.executable, os.path.join(self.root, "opt", "skirnir-router", "router.py"), "--init", conf,
                               "--public-url", "https://router.example.invalid:11435", "--cert", cert, "--key", key],
                              input="synthetic-password-1234\n", capture_output=True, text=True, env=self.env)
        self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
        rc, out, last = self.install()
        self.assertEqual((rc, last), (0, "STATE: installed"), out)
        self.assertIn("configuration ok", out)
        self.assertIn("POSTURE:", out)
        with open(os.path.join(conf, "config.yaml"), "w", encoding="utf-8") as f:
            f.write("router: {unknown_key: 1}\n")
        rc, out, last = self.install()
        self.assertEqual((rc, last), (2, "STATE: refused"), out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "opt", "skirnir-router", "router.py")), "program files stay installed")


if __name__ == "__main__":
    unittest.main(verbosity=1)
