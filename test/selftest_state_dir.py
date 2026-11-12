#!/usr/bin/env python3
"""Self-test (TM-14, ADR-0086): the router writes only into its state folder, and router-layout.sh moves it there.

1. router.state_dir: every file the router writes itself (roles.yaml, nodes.json, the journals, metrics, usage, perf, the
   Ollama release cache, the decision observation) lies in it; without the key, next to config.yaml as before; a relative
   path is refused; files root tools write (agent/, manifest.json) stay next to config.yaml.
2. router-layout.sh (Linux, bash; --root without owners): refuses while config.yaml lacks router.state_dir and changes
   nothing; then moves the router's files out of the configuration folder, removes a symlink there instead of following
   it, keeps an existing file in the state folder, leaves secrets.env where it is, and a second run reports the layout as
   current. Synthetic values only.
    python test/selftest_state_dir.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "router"))
from skirnir_router import (agentupdate, config, deletion_journal, fallback_observation, metrics, ollamaupdate,  # noqa: E402
                            ops, perf, state)
from skirnir_router.common import state_dir  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(ROOT, "router", "config.example.yaml")
LAYOUT = os.path.join(ROOT, "router", "router-layout.sh")


def write_config(folder, state_folder):
    raw = config.load_yaml(EXAMPLE)
    raw = yaml.safe_load(yaml.safe_dump(raw))
    if state_folder is None:
        raw["router"].pop("state_dir", None)
    else:
        raw["router"]["state_dir"] = state_folder
    path = os.path.join(folder, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f)
    return path


class StateDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conf = os.path.join(self.tmp, "etc")
        self.state = os.path.join(self.tmp, "var")
        os.makedirs(self.conf)
        os.makedirs(self.state)
        self.old_cfg = state.CFG
        self.addCleanup(setattr, state, "CFG", self.old_cfg)

    def written_paths(self):
        return {
            "roles.yaml": state.CFG.overrides_path,
            "usage.json": metrics.usage_path(),
            "metrics.json": metrics.metrics_path(),
            "perf.json": perf.perf_path(),
            "ollama-latest.json": ollamaupdate.latest_path(),
            "events.jsonl": state.decisions_path(),
            "deletions.jsonl": deletion_journal.path(),
            "requests.jsonl": fallback_observation.file_path(),
        }

    def test_files_follow_state_dir(self):
        # an absolute path in the configuration; posixpath form so the check holds on Windows too
        state_folder = self.state.replace(os.sep, "/")
        if not state_folder.startswith("/"):
            state_folder = "/" + state_folder.replace(":", "")
        path = write_config(self.conf, state_folder)
        cfg = config.Config.load(path, use_overrides=False)
        self.assertFalse(failed(cfg), getattr(cfg, "message", ""))
        state.CFG = cfg
        self.assertEqual(cfg.state_dir, state_folder)
        for name, p in self.written_paths().items():
            self.assertEqual(os.path.dirname(p), state_folder, name)
            self.assertEqual(os.path.basename(p), name)
        # written by root tools: stay next to config.yaml
        self.assertEqual(os.path.dirname(agentupdate.agent_dir()), os.path.dirname(os.path.abspath(path)))
        self.assertEqual(os.path.dirname(ops.manifest_path()), os.path.dirname(os.path.abspath(path)))

    def test_default_is_config_folder(self):
        path = write_config(self.conf, None)
        cfg = config.Config.load(path, use_overrides=False)
        self.assertFalse(failed(cfg))
        state.CFG = cfg
        for name, p in self.written_paths().items():
            self.assertEqual(os.path.dirname(p), os.path.dirname(os.path.abspath(path)), name)

    def test_relative_refused(self):
        cfg = config.Config.load(write_config(self.conf, "var/state"), use_overrides=False)
        self.assertTrue(failed(cfg))
        self.assertIn("state_dir", cfg.message)

    def test_helper_without_key(self):
        self.assertEqual(state_dir(SimpleNamespace(path="/x/config.yaml")), os.path.dirname(os.path.abspath("/x/config.yaml")))


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash") and shutil.which("python3"), "Linux, bash")
class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.c = os.path.join(self.root, "etc/skirnir-router")
        os.makedirs(os.path.join(self.c, "certs"))

    def run_layout(self, *args):
        r = subprocess.run(["bash", LAYOUT, "--root", self.root, *args], capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def put(self, rel, text="x\n"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_refuses_without_state_dir(self):
        self.put("etc/skirnir-router/config.yaml", "router:\n  listen: 127.0.0.1:1\n")
        self.put("etc/skirnir-router/roles.yaml")
        rc, out = self.run_layout()
        self.assertEqual(rc, 2, out)
        self.assertIn("router.state_dir", out)
        self.assertTrue(out.strip().endswith("STATE: refused"))
        self.assertTrue(os.path.exists(os.path.join(self.c, "roles.yaml")), "nothing moved")
        self.assertFalse(os.path.exists(os.path.join(self.root, "var/lib/skirnir-router")), "nothing created")

    def test_moves_and_is_idempotent(self):
        self.put("etc/skirnir-router/config.yaml", "router:\n  state_dir: /var/lib/skirnir-router\n")
        for name in ("roles.yaml", "nodes.json", "events.jsonl", "usage.json"):
            self.put(f"etc/skirnir-router/{name}", name + "\n")
        self.put("var/lib/skirnir-router/usage.json", "newer\n")            # exists already: kept
        os.symlink("/etc/hostname", os.path.join(self.c, "perf.json"))      # planted: removed, not followed
        self.put("etc/skirnir-router/secrets.env", "STAGING_ONLY=1\n")             # stays (root's, group-readable)
        self.put("etc/skirnir-router/agent/manifest.json", "{}\n")          # root's: stays
        rc, out = self.run_layout()
        self.assertEqual(rc, 0, out)
        s = os.path.join(self.root, "var/lib/skirnir-router")
        for name in ("roles.yaml", "nodes.json", "events.jsonl"):
            self.assertTrue(os.path.isfile(os.path.join(s, name)), name)
            self.assertFalse(os.path.exists(os.path.join(self.c, name)), name)
        with open(os.path.join(s, "usage.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "newer\n", "an existing file in the state folder is kept")
        self.assertFalse(os.path.lexists(os.path.join(self.c, "perf.json")), "planted symlink removed")
        self.assertFalse(os.path.exists(os.path.join(s, "perf.json")), "and not followed")
        self.assertTrue(os.path.isfile(os.path.join(self.c, "secrets.env")), "secrets.env stays in the configuration folder")
        self.assertTrue(os.path.isfile(os.path.join(self.c, "agent/manifest.json")))
        self.assertTrue(out.strip().endswith("STATE: done"))
        rc2, out2 = self.run_layout("--check")
        self.assertEqual(rc2, 0, out2)
        self.assertNotIn("moving", out2)
        self.assertTrue(out2.strip().endswith("STATE: checked"), out2)

    def test_refuses_symlinked_config_folder(self):
        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target, True)
        shutil.rmtree(self.c)
        os.symlink(target, self.c)
        rc, out = self.run_layout()
        self.assertEqual(rc, 2, out)
        self.assertIn("symlink", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
