"""Run the default-branch checker against a PR that replaces its checker and removes its workflow."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools/security" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("trusted_gate")
review = load("review")


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.PIPE, text=True).strip()


class TrustedGateTests(unittest.TestCase):
    def test_replaced_checker_cannot_self_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            origin = tmp / "origin"
            origin.mkdir()
            git(origin, "init", "-b", "main")
            git(origin, "config", "user.name", "Fixture")
            git(origin, "config", "user.email", "fixture@example.invalid")
            checker = "tools/security/review.py"
            for path in (checker, review.SKILL, review.MODEL, review.REGISTER, review.RECORD, ".github/workflows/threat-model.yml"):
                (origin / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, origin / path)
            register = json.loads((origin / review.REGISTER).read_text())
            for item in register["risks"]:
                item["status"] = "open"
                item["test_refs"] = []
            (origin / review.REGISTER).write_text(json.dumps(register))
            git(origin, "add", ".")
            git(origin, "commit", "-m", "base")
            base = git(origin, "rev-parse", "HEAD")
            trusted = tmp / "runner"
            git(tmp, "clone", str(origin), str(trusted))
            git(origin, "switch", "-c", "candidate")
            (origin / checker).write_text("from pathlib import Path\nPath('candidate-was-executed').touch()\n")
            (origin / ".github/workflows/threat-model.yml").unlink()
            git(origin, "add", "-A")
            git(origin, "commit", "-m", "replace checks")
            real_output = subprocess.check_output

            def execute(expected, *, move=False):
                head = git(origin, "rev-parse", "HEAD")
                git(origin, "update-ref", "refs/pull/7/head", base if move else head)
                pr = {"state": "open", "number": 7, "base": {"ref": "main", "sha": base, "repo": {"full_name": "example/project"}},
                      "head": {"sha": head}, "user": {"login": "contributor"}}

                def output(args, **kwargs):
                    return json.dumps([[pr]]).encode() if args[0] == "gh" else real_output(args, **kwargs)

                env = {"REVIEW_HEAD": head, "GITHUB_REPOSITORY": "example/project", "DEFAULT_BRANCH": "main",
                       "GITHUB_OUTPUT": str(tmp / "output")}
                with patch.dict(os.environ, env), contextlib.chdir(trusted), patch.object(subprocess, "check_output", side_effect=output):
                    self.assertEqual(gate.main(), expected)
                self.assertFalse((trusted / "candidate-was-executed").exists())

            execute(1)
            snap, errors = review.snapshot(base, "HEAD", origin)
            self.assertEqual(errors, [])
            evidence = {"schema_version": 1, "status": "completed", "reviewer": "fixture", "summary": "Synthetic review",
                        "base_sha": base, "change_digest": snap["change_digest"], "reviewed_paths": snap["changed_paths"],
                        "risk_ids": ["TM-16"], "boundaries": ["B9"], "findings": [], "limitations": ["fixture"],
                        "tests": [{"command": "fixture", "result": "not_run", "evidence": "Synthetic record for object isolation test"}]}
            (origin / review.RECORD).write_text(json.dumps(evidence))
            git(origin, "commit", "-am", "provide current evidence")
            execute(0)
            execute(1, move=True)


if __name__ == "__main__":
    unittest.main()
