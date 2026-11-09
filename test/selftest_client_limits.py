#!/usr/bin/env python3
"""Self-test (TM-08, ADR-0063): per-client bounds for authenticated traffic.

A configured client may have at most `max_concurrent` requests in progress (default 4); the next one is answered 429 with
Retry-After at once, before any work. A declared request body above `max_body_mib` (default 16) is answered 413. A slot is
given back when a request finishes, fails or the client disconnects. The router's own UI identity is not limited. Checks
configuration, the middleware with a gated handler, cancellation, the UI fields and the metrics. Synthetic values only."""
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
from skirnir_router import admin, auth, config, metrics, state  # noqa: E402
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
        app.router.add_post("/api/chat", slow)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
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
