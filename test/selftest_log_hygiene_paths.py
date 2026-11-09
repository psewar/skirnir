#!/usr/bin/env python3
"""Self-test (TM-15, ADR-0056): canaries on the memory, session and upstream error paths never reach the log or the journal.

selftest_log_hygiene.py covers authentication, inference without a node and token rotation. This test adds the paths
that carry user content or backend replies: automatic recall (prompt and recalled fact), retention of a curated fact and
of a model answer, a failing memory backend whose error body carries data, a failing inference backend likewise, and the
session API with a message and a store exception whose text carries data. Every canary is searched in every captured log
record and in the journal (events.jsonl). The instruments are checked too: the log and the journal must have recorded the
requests, otherwise an empty capture would pass. Synthetic values only; no network beyond loopback test servers."""
import hashlib
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from selftest_memory import PROJECT, MemoryHarness  # noqa: E402
from skirnir_router import app, config, session_api, state  # noqa: E402
from skirnir_router.session_config import SessionConfig  # noqa: E402
from skirnir_router.session_store import SessionStore  # noqa: E402

PROMPT = "canary-memory-prompt-3d7e15"
FACT = "canary-recalled-fact-81b2c9"
RETAINED = "canary-retained-content-6f0a27"
ANSWER = "canary-model-answer-29c4d1"
MEMORY_ERROR = "canary-memory-backend-error-c41e93"
UPSTREAM_ERROR = "canary-upstream-error-0b9d64"
SESSION_MESSAGE = "canary-session-message-5e2f70"
SESSION_METADATA = "canary-session-metadata-a83b06"
STORE_ERROR = "canary-store-exception-d17f52"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        # format() includes the traceback text of logged exceptions, where a leaked message would appear.
        self.lines.append(self.format(record))


class Hygiene:
    """Log capture and a private journal file around one test; check() searches both for canaries."""
    def start_capture(self):
        self.tmp = tempfile.mkdtemp(prefix="skirnir-hygiene-paths-")
        self.previous_journal = state.DECISIONS_FILE
        state.DECISIONS_FILE = os.path.join(self.tmp, "events.jsonl")
        self.capture = Capture()
        self.capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.root = logging.getLogger()
        self.previous_level = self.root.level
        self.root.addHandler(self.capture)
        self.root.setLevel(logging.DEBUG)

    def stop_capture(self):
        self.root.removeHandler(self.capture)
        self.root.setLevel(self.previous_level)
        state.DECISIONS_FILE = self.previous_journal
        shutil.rmtree(self.tmp, ignore_errors=True)

    def journal(self):
        path = os.path.join(self.tmp, "events.jsonl")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def assert_no_canary(self, canaries):
        logs, journal = "\n".join(self.capture.lines), self.journal()
        for where, text in (("log", logs), ("journal", journal)):
            for label, value in canaries.items():
                with self.subTest(canary=label, where=where):
                    self.assertNotIn(value, text, f"{label} leaked into the {where}")


class MemoryPathHygieneTests(MemoryHarness, Hygiene):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.start_capture()
        self.memory_error = False

    async def asyncTearDown(self):
        self.stop_capture()
        await super().asyncTearDown()

    async def fake_recall(self, request):
        if self.memory_error:
            await request.read()
            return web.Response(status=500, text=MEMORY_ERROR)
        return await super().fake_recall(request)

    async def fake_retain(self, request):
        if self.memory_error:
            await request.read()
            return web.Response(status=500, text=MEMORY_ERROR)
        return await super().fake_retain(request)

    async def fake_inference(self, request):
        if self.response_mode == "canary_error":
            self.upstream_bodies.append(await request.json())
            return web.json_response({"error": UPSTREAM_ERROR}, status=400)   # a 5xx makes the router try the next node
        if self.response_mode == "canary_answer":
            data = await request.json()
            self.upstream_bodies.append(data)
            message = {"role": "assistant", "content": ANSWER}
            return web.json_response({"model": "example-model", "message": message, "done": True, "done_reason": "stop"})
        return await super().fake_inference(request)

    async def test_memory_and_upstream_paths_keep_canaries_out_of_log_and_journal(self):
        messages = [{"role": "user", "content": PROMPT}]
        self.results = [{"id": "source-1", "text": FACT}]
        # Automatic recall: the prompt goes to the memory backend as the query, the fact into the prompt.
        r = await self.infer(extra={"messages": messages})
        self.assertEqual((r.status, r.headers.get("X-Skirnir-Memory-State")), (200, "available"))
        self.assertIn(FACT, str(self.upstream_bodies[-1]["messages"]), "instrument: the fact reached the model")
        # Retention of a curated fact through the REST API, and of a model answer through inference.
        data = {"project": PROJECT, "event_id": "canary-curated", "content": RETAINED, "confirmed": True}
        self.assertEqual((await self.call("retain", data=data)).status, 202)
        self.response_mode = "canary_answer"
        r = await self.infer(extra={"messages": messages}, retain=True, eid="canary-answer")
        self.assertEqual(r.status, 200)
        await self.service.queue.join()
        # A failing memory backend whose error body carries data: explicit recall answers 503, inference proceeds.
        self.memory_error = True
        self.assertEqual((await self.call(data={"project": PROJECT, "query": PROMPT})).status, 503)
        self.response_mode = "normal"
        r = await self.infer(extra={"messages": messages})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers.get("X-Skirnir-Memory-State", "").startswith("unavailable"))
        self.memory_error = False
        # A failing inference backend whose error body carries data.
        self.response_mode = "canary_error"
        r = await self.infer(extra={"messages": messages})
        self.assertGreaterEqual(r.status, 400)
        self.response_mode = "normal"
        # Instruments: requests were logged and journaled, so an empty capture cannot pass.
        self.assertTrue(self.capture.lines, "instrument: the log captured records")
        journal = self.journal()
        self.assertIn('"event": "route"', journal, "instrument: the journal recorded the requests")
        self.assertIn('"memory_state": "available"', journal, "instrument: the journal recorded the memory state")
        self.assert_no_canary({"prompt": PROMPT, "recalled fact": FACT, "retained content": RETAINED, "model answer": ANSWER,
                               "memory backend error": MEMORY_ERROR, "upstream error": UPSTREAM_ERROR})


class SessionPathHygieneTests(unittest.IsolatedAsyncioTestCase, Hygiene):
    async def asyncSetUp(self):
        self.old_cfg, self.old_mode = state.CFG, state.CLIENT_AUTH_MODE
        cfg = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
        cfg.session_store = SessionConfig(enabled=True)
        cfg.client_auth = {"mode": "observe", "audit_log": None, "clients": {
            "alice": {"token_sha256": hashlib.sha256(b"alice").hexdigest(), "session_projects": ["work"]}}}
        state.CFG, state.CLIENT_AUTH_MODE = cfg, None
        self.store = AsyncMock(spec=SessionStore)
        self.sid = str(uuid4())
        self.session = {"id": self.sid, "owner": "alice", "project": "work", "mode": "client-managed", "status": "active"}
        self.store.create.return_value = self.session
        self.store.get.return_value = self.session
        self.store.append.return_value = ({"seq": 1, "event_id": "one"}, True)
        self.patches = [patch.object(session_api.PostgresSessionStore, "open", return_value=self.store),
                        patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": "test-only"})]
        for p in self.patches:
            p.start()
        self.start_capture()
        api, _ = app.build_apps()
        self.client = TestClient(TestServer(api))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.stop_capture()
        for p in reversed(self.patches):
            p.stop()
        state.CFG, state.CLIENT_AUTH_MODE = self.old_cfg, self.old_mode

    async def request(self, method, path, **kwargs):
        return await self.client.request(method, path, headers={"Authorization": "Bearer alice"}, **kwargs)

    async def test_session_paths_keep_canaries_out_of_log_and_journal(self):
        url = session_api.PREFIX
        r = await self.request("POST", url, json={"project": "work", "metadata": {"note": SESSION_METADATA}})
        self.assertEqual(r.status, 201)
        r = await self.request("POST", f"{url}/{self.sid}/messages",
                               json={"event_id": "one", "message": {"role": "user", "content": SESSION_MESSAGE}})
        self.assertEqual(r.status, 201)
        self.assertIn(SESSION_MESSAGE, str(self.store.append.await_args), "instrument: the message reached the store")
        # A store exception whose text carries data (as a database error quoting a value would).
        self.store.get.side_effect = RuntimeError(STORE_ERROR)
        r = await self.request("GET", f"{url}/{self.sid}")
        self.assertGreaterEqual(r.status, 500)
        self.assert_no_canary({"store exception": STORE_ERROR})   # first, so a leak is reported as such
        body = await r.json()
        self.assertEqual((r.status, body["error"]["type"]), (500, "internal_error"))
        self.assertNotIn(STORE_ERROR, str(body), "the client answer does not carry the exception text")
        # Instrument: the error path was logged, with type and the same reference, but without the message.
        self.assertTrue(any("unexpected RuntimeError" in line and body["error"]["ref"] in line for line in self.capture.lines),
                        "the log names the exception type and reference")
        self.assertTrue(self.capture.lines, "instrument: the log captured records")
        self.assert_no_canary({"session message": SESSION_MESSAGE, "session metadata": SESSION_METADATA,
                               "store exception": STORE_ERROR})


if __name__ == "__main__":
    unittest.main(verbosity=1)
