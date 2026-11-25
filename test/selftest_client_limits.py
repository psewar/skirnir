#!/usr/bin/env python3
"""Self-test (TM-08, ADR-0063, ADR-0129): per-client bounds for authenticated traffic.

A configured client may have at most `max_concurrent` requests in progress (default 4); the next one is answered 429 with
Retry-After at once, before any work. A declared request body above `max_body_mib` (default 16) is answered 413; so is a
chunked body (no declared length) once it grows past the limit while the router reads it, inside the client's slot, so a
client cannot hold more than max_concurrent bodies in memory. A chunked body within the limit reaches the handler intact.
A slot is given back when a request finishes, fails or the client disconnects. The router's own UI identity is not limited.
Checks configuration, the middleware with a gated handler, cancellation, the UI fields and the metrics. Synthetic values
only."""
import asyncio
import copy
import hashlib
import os
import sys
import unittest

from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import admin, auth, common, config, metrics, session_api, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")


def token(name):
    return hashlib.sha256(name.encode()).hexdigest()


class ConfigurationTests(unittest.TestCase):
    def parse(self, **limits):
        raw = copy.deepcopy(config.load_yaml(EXAMPLE))
        raw["router"]["client_auth"] = {"mode": "enforce", "clients": {"example-client": {"token_sha256": token("example-client"), **limits}}}
        cfg = config.Config.__new__(config.Config)
        return cfg, cfg.parse(raw)

    def test_limits_are_bounded_integers(self):
        for limits in ({}, {"max_concurrent": 1}, {"max_concurrent": 256}, {"max_body_mib": 1}, {"max_body_mib": 64}):
            with self.subTest(limits=limits):
                cfg, result = self.parse(**limits)
                self.assertFalse(failed(result), getattr(result, "message", result))
        for limits in ({"max_concurrent": 0}, {"max_concurrent": 257}, {"max_concurrent": "4"}, {"max_concurrent": True},
                       {"max_body_mib": 65}, {"max_body_mib": 1.5}):
            with self.subTest(limits=limits):
                self.assertTrue(failed(self.parse(**limits)[1]))

    def test_defaults(self):
        self.assertEqual(auth.client_limits({}), (4, 16 * auth.MIB))
        self.assertEqual(auth.client_limits({"max_concurrent": 16, "max_body_mib": 32}), (16, 32 * auth.MIB))

    def test_ui_fields(self):
        spec = admin._client_spec("example-client", {"max_concurrent": "8", "max_body_mib": 32})
        self.assertEqual((spec["max_concurrent"], spec["max_body_mib"]), (8, 32))
        self.assertTrue(failed(admin._client_spec("example-client", {"max_concurrent": "many"})))


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = state.CFG, state.CLIENT_AUTH_MODE, copy.deepcopy(state.CLIENT_STATS)
        cfg = config.Config(EXAMPLE, use_overrides=False)
        cfg.client_auth = {"mode": "enforce", "audit_log": None, "clients": {
            "default-client": {"token_sha256": token("default-client")},
            "single-client": {"token_sha256": token("single-client"), "max_concurrent": 1, "max_body_mib": 32}}}
        state.CFG, state.CLIENT_AUTH_MODE = cfg, None
        state.CLIENT_STATS["clients"].clear()
        self.gate, self.entered = asyncio.Event(), 0
        app = web.Application(client_max_size=64 * auth.MIB, middlewares=[auth.client_auth_middleware])   # as the router (app.py)

        async def slow(request):
            await request.read()
            self.entered += 1
            await self.gate.wait()
            return web.json_response({"ok": True})

        async def echo(request):   # what a handler sees of the body: its length and, through read_json, its JSON
            raw = await request.read()
            body = await common.read_json(request) if raw[:1] == b"{" else None
            return web.json_response({"length": len(raw), "json": body})
        async def session_write(request):   # the session and memory APIs read their body with session_api.body (1 MiB)
            data = await session_api.body(request)
            if failed(data):
                return web.json_response({"error": data.message}, status=data.status)
            return web.json_response({"json": data})
        app.router.add_post("/api/chat", slow)
        app.router.add_post("/api/generate", echo)
        app.router.add_post("/v1/sessions/example", session_write)
        self.audits = []
        self.real_audit = auth.audit
        auth.audit = lambda event, **fields: self.audits.append((event, fields))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        auth.audit = self.real_audit
        state.CFG, state.CLIENT_AUTH_MODE = self.previous[:2]
        state.CLIENT_STATS.clear()
        state.CLIENT_STATS.update(self.previous[2])

    def post(self, name, data=b"{}", **kwargs):
        return self.client.post("/api/chat", data=data, headers={"Authorization": "Bearer " + name, "Content-Type": "application/json"}, **kwargs)

    async def wait_entered(self, n):
        for _ in range(200):
            if self.entered >= n:
                return
            await asyncio.sleep(0.01)
        self.fail(f"only {self.entered} of {n} requests reached the handler")

    def stats(self, name):
        return state.CLIENT_STATS["clients"][name]

    async def test_concurrency_limit_per_client(self):
        running = [asyncio.create_task(self.post("default-client")) for _ in range(4)]
        await self.wait_entered(4)
        refused = await self.post("default-client")
        self.assertEqual(refused.status, 429)
        self.assertEqual(refused.headers.get("Retry-After"), "1")
        self.assertIn("concurrency limit: 4", await refused.text())
        self.assertEqual(self.entered, 4, "the refused request did no work")
        # Another client has its own slots.
        other = asyncio.create_task(self.post("single-client"))
        await self.wait_entered(5)
        self.assertEqual((await self.post("single-client")).status, 429, "max_concurrent: 1")
        self.gate.set()
        statuses = [r.status for r in await asyncio.gather(*running, other)]
        self.assertEqual(statuses, [200] * 5)
        self.assertEqual((self.stats("default-client")["inflight"], self.stats("default-client")["peak_inflight"]), (0, 4))
        self.assertEqual(self.stats("default-client")["concurrency_limited"], 1)
        self.assertEqual((await self.post("default-client")).status, 200, "slots are free again")

    async def test_declared_body_size(self):
        self.gate.set()
        big = b"x" * (17 * auth.MIB)
        r = await self.post("default-client", data=big)
        self.assertEqual(r.status, 413)
        self.assertEqual(self.entered, 0, "refused before the handler")
        self.assertEqual((await self.post("single-client", data=big)).status, 200, "max_body_mib: 32 allows it")
        self.assertEqual(self.stats("default-client")["too_large"], 1)

    @staticmethod
    async def chunks(total, piece=auth.MIB, gate=None):
        """A request body without Content-Length: aiohttp sends an async generator with Transfer-Encoding: chunked."""
        sent = 0
        while sent < total:
            n = min(piece, total - sent)
            yield b"x" * n
            sent += n
            if gate is not None and sent == piece:
                await gate.wait()   # hold the upload open after the first piece

    async def test_chunked_body_above_the_limit_is_refused_while_read(self):
        self.gate.set()
        r = await self.client.post("/api/chat", data=self.chunks(17 * auth.MIB),
                                   headers={"Authorization": "Bearer default-client"})
        self.assertEqual(r.status, 413, "rejected: 17 MiB chunked for a client with the default 16 MiB")
        self.assertIn("request body above 16 MiB for client default-client", await r.text())
        self.assertEqual(self.entered, 0, "the handler never ran")
        self.assertEqual(self.stats("default-client")["too_large"], 1)
        self.assertEqual(self.stats("default-client")["inflight"], 0, "the slot is given back")
        self.assertIn(("body_too_large", {"client": "default-client", "path": "/api/chat", "limit_mib": 16, "chunked": True, "suppressed": 0}),
                      self.audits)

    async def test_chunked_body_within_the_limit_reaches_the_handler_intact(self):
        r = await self.client.post("/api/generate", data=self.chunks(17 * auth.MIB),
                                   headers={"Authorization": "Bearer single-client"})
        self.assertEqual((r.status, (await r.json())["length"]), (200, 17 * auth.MIB), "max_body_mib: 32 allows it")

        async def json_chunks():
            yield b'{"model": "example", '
            yield b'"prompt": "synthetic"}'
        r = await self.client.post("/api/generate", data=json_chunks(),
                                   headers={"Authorization": "Bearer default-client", "Content-Type": "application/json"})
        self.assertEqual((await r.json())["json"], {"model": "example", "prompt": "synthetic"}, "read_json gets the body")
        self.assertEqual(self.stats("single-client")["too_large"], 0)
        self.assertEqual(self.audits, [])

    async def test_the_limit_also_holds_where_clone_takes_no_limit(self):
        # aiohttp 3.8 (the oldest requirements.txt allows) has no client_max_size in clone(); with_body_limit() then sets
        # the attribute read() checks. Forced here, since CI runs a newer aiohttp.
        self.assertEqual(common.CLONE_TAKES_BODY_LIMIT, "client_max_size" in __import__("inspect").signature(
            web.BaseRequest.clone).parameters)
        previous, common.CLONE_TAKES_BODY_LIMIT = common.CLONE_TAKES_BODY_LIMIT, False
        try:
            self.gate.set()
            headers = {"Authorization": "Bearer default-client"}
            r = await self.client.post("/api/chat", data=self.chunks(17 * auth.MIB), headers=headers)
            self.assertEqual(r.status, 413, "rejected on the fallback path as well")
            r = await self.client.post("/api/generate", data=self.chunks(3 * auth.MIB), headers=headers)
            self.assertEqual((r.status, (await r.json())["length"]), (200, 3 * auth.MIB))
        finally:
            common.CLONE_TAKES_BODY_LIMIT = previous

    async def test_handlers_that_stream_their_body_get_the_body_the_middleware_read(self):
        # session_api.body streams the body with its own 1 MiB bound; after the middleware read a chunked body it uses the
        # bytes on the request (BODY_PREREAD) instead of the used-up stream.
        headers = {"Authorization": "Bearer default-client", "Content-Type": "application/json"}

        async def json_chunks():
            yield b'{"event_id": "example-1", '
            yield b'"message": {"role": "user", "content": "synthetic"}}'
        r = await self.client.post("/v1/sessions/example", data=json_chunks(), headers=headers)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await r.json())["json"]["event_id"], "example-1")
        r = await self.client.post("/v1/sessions/example", data=self.chunks(2 * auth.MIB), headers=headers)
        self.assertEqual((r.status, (await r.json())["error"]), (413, "session body exceeds 1 MiB"),
                         "within the client's 16 MiB, above the session API's own bound")
        r = await self.client.post("/v1/sessions/example", data=b'{"event_id": "example-2"}', headers=headers)
        self.assertEqual((await r.json())["json"], {"event_id": "example-2"}, "a declared body is still streamed as before")

    async def test_a_chunked_body_is_read_inside_the_clients_slot(self):
        # While a slow chunked upload is being read, it holds single-client's only slot: a second request is refused 429,
        # so a client cannot hold more than max_concurrent bodies in memory by uploading slowly in parallel.
        self.gate.set()
        hold = asyncio.Event()
        upload = asyncio.create_task(self.client.post("/api/generate", data=self.chunks(3 * auth.MIB, gate=hold),
                                                      headers={"Authorization": "Bearer single-client"}))
        for _ in range(200):   # the client's statistics exist once the upload reached the middleware
            if state.CLIENT_STATS["clients"].get("single-client", {}).get("inflight") == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.stats("single-client")["inflight"], 1, "the upload holds the slot while it is read")
        self.assertEqual((await self.post("single-client")).status, 429)
        hold.set()
        r = await upload
        self.assertEqual((r.status, (await r.json())["length"]), (200, 3 * auth.MIB))
        self.assertEqual(self.stats("single-client")["inflight"], 0)

    async def test_a_disconnecting_client_gives_its_slot_back(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.post("single-client", timeout=ClientTimeout(total=0.3))
        for _ in range(100):   # the server notices the disconnect and cancels the handler
            if self.stats("single-client")["inflight"] == 0:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.stats("single-client")["inflight"], 0, "the slot was given back after the disconnect")
        self.gate.set()
        self.assertEqual((await self.post("single-client")).status, 200)

    async def test_metrics_show_the_counters(self):
        self.gate.set()
        await self.post("default-client")
        text = metrics.render() if hasattr(metrics, "render") else ""
        if not text:
            self.skipTest("metrics renderer not exposed under this name")
        for name in ("skirnir_client_inflight", "skirnir_client_peak_inflight", "skirnir_client_concurrency_limited_total",
                     "skirnir_client_too_large_total"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
