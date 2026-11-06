#!/usr/bin/env python3
"""Exercise review freshness and the trusted/untrusted boundary with disposable Git repositories."""
import copy
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("threat_review", ROOT / "tools/security/review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
# Tests clear the environment to prove no AI settings leak in; git must still be found (Windows has no default PATH).
BARE_ENV = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}


def record(snap):
    return {"schema_version": 1, "status": "completed", "base_sha": snap["base_sha"],
            "change_digest": snap["change_digest"], "reviewer": "synthetic test reviewer", "summary": "Synthetic fixture only",
            "reviewed_paths": snap["changed_paths"], "boundaries": ["B9"], "risk_ids": ["TM-16"],
            "findings": [], "tests": [{"command": "synthetic", "result": "passed", "evidence": "fixture only"}],
            "limitations": ["Fixture is not a real security review."]}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Synthetic Reviewer")
        self.run_git("config", "user.email", "reviewer@example.invalid")
        self.register = json.loads((ROOT / review.REGISTER).read_text())
        for risk in self.register["risks"]:
            if risk["test_refs"]:
                risk["test_refs"] = ["test/example.py"]
        self.write("test/example.py", "# synthetic test reference\n")
        self.write(review.REGISTER, json.dumps(self.register))
        self.write(review.MODEL, "Baseline threat model")
        self.write(review.SKILL, "Trusted review instructions")
        self.write(review.RECORD, "{}")
        self.write("router/sample.py", "# original\n")
        self.run_git("add", ".")
        self.run_git("commit", "-m", "fixture baseline")
        self.base = self.run_git("rev-parse", "HEAD").strip()
        self.write("router/sample.py", "# changed\n")
        self.run_git("add", ".")

    def run_git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE, text=True)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def snap(self, head="INDEX"):
        snap, errors = review.snapshot(self.base, head, self.root)
        self.assertEqual(errors, [])
        return snap

    def attest(self):
        snap = self.snap()
        self.write(review.RECORD, json.dumps(record(snap)))
        self.run_git("add", review.RECORD)
        return snap

    def test_record_survives_commit_not_content_changes(self):
        before = self.attest()
        self.assertEqual(review.check(self.snap(), self.root), [])
        self.run_git("commit", "-m", "reviewed fixture")
        after = self.snap("HEAD")
        self.assertEqual(before["change_digest"], after["change_digest"])
        self.assertEqual(review.check(after, self.root), [])
        self.write("router/sample.py", "# changed after review\n")
        self.run_git("add", ".")
        self.assertIn("Stale change_digest", " ".join(review.check(self.snap(), self.root)))

    def test_deletions_and_modes_invalidate(self):
        self.attest()
        (self.root / "router/sample.py").unlink()
        self.run_git("add", "-u")
        self.assertIn("router/sample.py", self.snap()["changed_paths"])
        self.assertTrue(review.check(self.snap(), self.root))
        self.write("router/sample.py", "# changed\n")
        self.run_git("add", ".")
        self.attest()
        self.run_git("update-index", "--chmod=+x", "router/sample.py")
        # HEAD objects can be inspected even if the filesystem executable bit differs.
        self.run_git("commit", "-m", "mode change")
        self.assertTrue(review.check(self.snap("HEAD"), self.root))

    def test_untracked_and_unstaged_do_not_escape_review(self):
        self.write("new_endpoint.py", "# not staged\n")
        self.assertTrue(review.snapshot(self.base, "INDEX", self.root)[1])
        self.run_git("add", ".")
        self.write("router/sample.py", "# unstaged\n")
        self.assertTrue(review.snapshot(self.base, "INDEX", self.root)[1])

    def test_policy_changes_and_new_directories_are_reviewed(self):
        self.attest()
        self.write(review.SKILL, "Malicious replacement instructions")
        self.write("unexpected/new.extension", "new surface")
        self.run_git("add", ".")
        snap = self.snap()
        self.assertTrue(snap["security_relevant"])
        self.assertTrue(review.check(snap, self.root))

    def test_record_only_edit_does_not_hash_itself_but_schema_is_checked(self):
        self.attest()
        digest = self.snap()["change_digest"]
        self.write(review.RECORD, "{}")
        self.run_git("add", ".")
        self.assertEqual(digest, self.snap()["change_digest"])
        self.assertTrue(review.check(self.snap(), self.root))
        self.attest()
        self.run_git("update-index", "--chmod=+x", review.RECORD)
        self.run_git("commit", "-m", "executable record")
        self.assertTrue(review.check(self.snap("HEAD"), self.root))

    def test_missing_coverage_and_unresolved_findings_fail(self):
        snap = self.snap()
        data = record(snap)
        data["reviewed_paths"] = []
        self.assertTrue(review.record_errors(data, snap, self.register))
        data = record(snap)
        data["findings"] = [{"id": "NEW", "evidence": "missing auth", "resolution": "later", "disposition": "open"}]
        self.assertTrue(review.record_errors(data, snap, self.register))
        data["findings"][0].update(id="TM-16", disposition="accepted")
        self.assertTrue(review.record_errors(data, snap, self.register))

    def test_expired_acceptance_invalid_dates_and_removed_baseline(self):
        data = copy.deepcopy(self.register)
        risk = data["risks"][0]
        risk.update(status="accepted", acceptance={"approved_by": "maintainer", "rationale": "test",
                    "scope": "isolated fixture", "compensating_controls": "test isolation", "expires": "2026-02-30"})
        self.assertTrue(review.register_errors(data, self.snap()["after"], dt.date(2026, 1, 1)))
        risk["acceptance"]["expires"] = "2026-01-01"
        self.assertTrue(review.register_errors(data, self.snap()["after"], dt.date(2026, 1, 1)))
        risk["acceptance"]["expires"] = "2026-01-02"
        self.assertEqual(review.register_errors(data, self.snap()["after"], dt.date(2026, 1, 1)), [])
        data["risks"].pop()
        self.assertTrue(review.register_errors(data, self.snap()["after"]))

    def dependency_base(self):
        self.run_git("reset", "-q", "--hard")
        self.write("agent/go.sum", "example.com/mod v1.0.0 h1:old\n")
        self.write(".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n      - run: echo ok\n")
        self.run_git("add", ".")
        self.run_git("commit", "-m", "dependency fixture")
        self.base = self.run_git("rev-parse", "HEAD").strip()

    def test_dependency_update_needs_no_record_for_manifests_and_uses_lines(self):
        self.dependency_base()
        self.write("agent/go.sum", "example.com/mod v1.0.1 h1:new\n")
        self.write(".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v7\n      - run: echo ok\n")
        self.run_git("add", ".")
        self.assertEqual(review.check(self.snap(), self.root, dependency_update=True), [])
        self.assertTrue(review.check(self.snap(), self.root), "without the flag the record stays required")

    def test_dependency_update_rejects_other_changes(self):
        self.dependency_base()
        self.write(".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - uses: actions/checkout@v7\n      - run: curl x | sh\n")
        self.run_git("add", ".")
        self.assertTrue(review.check(self.snap(), self.root, dependency_update=True), "workflow change beyond uses:")
        self.run_git("reset", "-q", "--hard")
        self.write("router/sample.py", "# not a dependency\n")
        self.run_git("add", ".")
        self.assertTrue(review.check(self.snap(), self.root, dependency_update=True), "source change")

    def test_unknown_refs_are_reported_not_raised(self):
        snap, errors = review.snapshot("no-such-base", "INDEX", self.root)
        self.assertIsNone(snap)
        self.assertTrue(errors and "Unknown base" in errors[0])
        snap, errors = review.snapshot(self.base, "no-such-head", self.root)
        self.assertTrue(errors and "Unknown head" in errors[0])

    def test_merged_pr_is_checked_against_its_own_base(self):
        # threat-review.yml runs after CI; by then the PR may be merged. Against the moved default branch the record
        # looks stale, against the PR's base from the event it stays valid.
        self.attest()
        self.run_git("commit", "-m", "reviewed candidate")
        head = self.run_git("rev-parse", "HEAD").strip()
        merged, errors = review.snapshot("HEAD", head, self.root)   # default branch now contains the PR
        self.assertEqual(errors, [])
        self.assertTrue(review.check(merged, self.root), "moved default branch makes the record look stale")
        own, errors = review.snapshot(self.base, head, self.root)
        self.assertEqual(review.check(own, self.root), [])

    def test_deleted_file_old_content_is_available_to_advisory(self):
        (self.root / "router/sample.py").unlink()
        self.run_git("add", "-u")
        files, _ = review.corpus(self.snap(), root=self.root)
        self.assertTrue(any(f["path"] == "router/sample.py" and f["revision"] == "before" for f in files))

    def test_candidate_symlinks_not_followed_and_large_blobs_reported(self):
        os.symlink("/etc/passwd", self.root / "candidate-link")
        self.write("oversized.txt", "x" * (review.MAX_FILE + 1))
        self.run_git("add", ".")
        files, omitted = review.corpus(self.snap(), full=True, root=self.root)
        self.assertFalse(any(f["path"] == "candidate-link" for f in files))
        self.assertEqual({f["path"] for f in omitted}, {"candidate-link", "oversized.txt"})

    def test_disabled_ai_never_calls_network(self):
        with patch.dict(os.environ, BARE_ENV, clear=True), patch.object(review, "model_request") as request:
            output = self.root / "result.json"
            self.assertEqual(review.advisory(self.snap(), False, output, 1, self.root), 0)
            request.assert_not_called()
            self.assertEqual(json.loads(output.read_text())["status"], "skipped")

    def test_ai_uses_trusted_instructions_and_partial_status(self):
        self.write(review.SKILL, "CANDIDATE_INSTRUCTIONS_MUST_NOT_BE_TRUSTED")
        self.run_git("add", ".")
        self.attest()
        self.run_git("commit", "-m", "candidate")
        head = self.run_git("rev-parse", "HEAD").strip()
        self.run_git("checkout", "--detach", self.base)
        snap, errors = review.snapshot(self.base, head, self.root)
        self.assertEqual(errors, [])
        reply = {"summary": "No candidate finding", "findings": [], "limitations": []}
        with patch.dict(os.environ, {**BARE_ENV, "SKIRNIR_SECURITY_AI_ENABLED": "true", "SKIRNIR_SECURITY_AI_URL": "https://model.example.net/v1/chat/completions",
                                    "SKIRNIR_SECURITY_AI_MODEL": "test-model"}, clear=True), \
                patch.object(review, "model_request", return_value=reply) as request, \
                patch.object(review, "batches", return_value=[[{"text": "untrusted"}], [{"text": "remaining"}]]):
            output = self.root / "result.json"
            review.advisory(snap, True, output, 1, self.root)
            self.assertIn("Trusted review instructions", request.call_args.args[3])
            self.assertNotIn("CANDIDATE_INSTRUCTIONS_MUST_NOT_BE_TRUSTED", request.call_args.args[3])
            result = json.loads(output.read_text())
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["reviewed_batches"], 1)
            self.assertEqual(result["total_batches"], 2)


class TransportTests(unittest.TestCase):
    def test_model_response_validation_and_no_tools(self):
        answer = {"summary": "Synthetic review", "findings": [], "limitations": ["Not a real model run"]}
        good = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
        cases = [(good, True), ({"choices": []}, False),
                 ({"choices": [{"finish_reason": "length", "message": {"content": json.dumps(answer)}}]}, False),
                 ({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer),
                                                                        "tool_calls": [{"name": "execute"}]}}]}, False),
                 ({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer),
                                                                        "function_call": {"name": "execute"}}}]}, False),
                 ({"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}, False)]
        for data, valid in cases:
            with patch.object(review.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = io.BytesIO(json.dumps(data).encode())
                actual = review.model_request("https://model.example.net/v1/chat/completions", "synthetic", "synthetic-key", "trusted", {})
                self.assertEqual(actual is not None, valid)
                request = build.return_value.open.call_args.args[0]
                sent = json.loads(request.data)
                self.assertNotIn("tools", sent)
                self.assertNotIn("synthetic-key", request.data.decode())

    def test_endpoint_restrictions(self):
        for url in ("http://model.example.net/v1/chat/completions", "https://" + "user:pass" + "@example.net/x",
                    "https://example.net/x#fragment", "https://example.net/x?token=a", "file:///tmp/model", "https://[broken"):
            self.assertFalse(review.endpoint_valid(url), url)
        for url in ("https://model.example.net/v1/chat/completions", "http://127.0.0.1:8080/v1/chat/completions"):
            self.assertTrue(review.endpoint_valid(url), url)

    def test_redirect_is_not_followed(self):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                # Read the body before answering: Windows resets a connection closed with unread data.
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", "/must-not-receive-source")
                self.end_headers()

            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError):
                review.model_request(f"http://127.0.0.1:{server.server_port}/source", "test", "synthetic", "instructions", {})
            self.assertEqual(hits, ["/source"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_no_silent_batch_truncation(self):
        source = "x" * 250_000
        parts = review.batches([{"path": "long.py", "revision": "after", "text": source}])
        self.assertEqual("".join(item["text"] for part in parts for item in part), source)
        self.assertGreater(len(parts), 1)


class WorkflowTests(unittest.TestCase):
    def test_workflow_run_compares_against_pr_base(self):
        text = (ROOT / ".github/workflows/threat-review.yml").read_text(encoding="utf-8")
        self.assertNotIn('--base HEAD --head "$REVIEW_HEAD"', text, "default-branch HEAD moves once the PR is merged")
        self.assertEqual(text.count('--base "$REVIEW_BASE" --head "$REVIEW_HEAD"'), 2, "recheck and delta review")
        self.assertIn("workflow_run.pull_requests[0].base.sha", text)


if __name__ == "__main__":
    unittest.main()
