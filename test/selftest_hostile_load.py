#!/usr/bin/env python3
"""Self-test (TM-08, ADR-0063, ADR-0134): a hostile but identified client floods the router.

In process, over the real client middleware with a held handler (no network, no node): a client with a valid token
sends hundreds of requests at once and keeps going after it is refused. Checked under that load:
  - at most max_concurrent of its requests reach the handler; every other one is refused at once with 429, before any
    work and before its body is read;
  - another client is still served while the flood holds the hostile client's slots;
  - the audit trail stays bounded (ADR-0134): one line per client and event kind per CLIENT_AUDIT_EVERY_S, the next line
    reports how many were withheld, while the counters count every refusal;
  - the same for a rate-limited flood and for a flood of requests for a role the client may not use.
Synthetic values only."""
import asyncio
import copy
import hashlib
import os
import sys
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import auth, config, state  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
FLOOD = 200


def token(name):
    return hashlib.sha256(name.encode()).hexdigest()


class HostileLoadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = state.CFG, state.CLIENT_AUTH_MODE, copy.deepcopy(state.CLIENT_STATS)
        cfg = config.Config(EXAMPLE, use_overrides=False)
        self.roles = list(cfg.roles.values())
        cfg.client_auth = {"mode": "enforce", "audit_log": None, "clients": {
            "hostile": {"token_sha256": token("hostile"), "max_concurrent": 4},
            "legit": {"token_sha256": token("legit"), "max_concurrent": 2},
            "chatty": {"token_sha256": token("chatty"), "requests_per_minute": 5},
            "narrow": {"token_sha256": token("narrow"), "roles": [self.roles[0]["name"]]}}}
        state.CFG, state.CLIENT_AUTH_MODE = cfg, None
        state.CLIENT_STATS["clients"].clear()
        self.gate = asyncio.Event()
        self.inside = {"hostile": 0, "legit": 0}
        self.peak = {"hostile": 0, "legit": 0}
        self.bodies_read = 0
        app = web.Application(client_max_size=64 * auth.MIB, middlewares=[auth.client_auth_middleware])

        async def held(request):   # the hostile client's requests wait here; the legit client's do not
            name = request["client"]
            await request.read()
            self.bodies_read += 1
            self.inside[name] += 1
            self.peak[name] = max(self.peak[name], self.inside[name])
            if name == "hostile":
                await self.gate.wait()
            self.inside[name] -= 1
            return web.json_response({"ok": True})

        async def role_call(request):   # what the proxy does for a role: the request path's grant check
            refused = auth.client_allows(request, self.roles[1], concrete=False)
            return web.json_response({"refused": refused}, status=403 if refused else 200)

        app.router.add_post("/api/chat", held)
        app.router.add_post("/api/generate", role_call)
        self.audits = []
        self.real_audit = auth.audit
        auth.audit = lambda event, **fields: self.audits.append((event, fields))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.gate.set()
        await self.client.close()
        auth.audit = self.real_audit
        state.CFG, state.CLIENT_AUTH_MODE = self.previous[:2]
        state.CLIENT_STATS.clear()
        state.CLIENT_STATS.update(self.previous[2])

    async def post(self, name, path="/api/chat", body=b'{"messages": []}'):
        r = await self.client.post(path, data=body, headers={"Authorization": "Bearer " + name,
                                                               "Content-Type": "application/json"})
        await r.read()
        return r.status

    def lines(self, event, client):
        return [f for e, f in self.audits if e == event and f.get("client") == client]

    async def wait_inside(self, name, n):
        for _ in range(300):
            if self.inside[name] >= n:
                return
            await asyncio.sleep(0.01)
        self.fail(f"only {self.inside[name]} of {n} {name} requests reached the handler")

    async def test_a_flood_is_bounded_and_does_not_starve_another_client(self):
        held = [asyncio.create_task(self.post("hostile")) for _ in range(4)]
        await self.wait_inside("hostile", 4)
        body = b'{"messages": [], "pad": "' + b"x" * 65536 + b'"}'
        refused = await asyncio.gather(*(self.post("hostile", body=body) for _ in range(FLOOD)))
        self.assertEqual(refused, [429] * FLOOD, "every request beyond the client's slots is refused")
        self.assertEqual((self.peak["hostile"], self.bodies_read), (4, 4), "never more than 4 inside; refused bodies unread")
        # Another client is served while the flood holds the hostile client's slots.
        self.assertEqual(await asyncio.gather(*(self.post("legit") for _ in range(3))), [200, 200, 200])
        stats = state.CLIENT_STATS["clients"]["hostile"]
        self.assertEqual(stats["concurrency_limited"], FLOOD, "the counter counts every refusal")
        lines = self.lines("concurrency_limited", "hostile")
        self.assertEqual(len(lines), 1, "one audit line for the whole flood (ADR-0134)")
        self.assertEqual(lines[0]["suppressed"], 0)
        # Once the interval has passed, the next refusal writes a line that reports what was withheld.
        stats["last_audit"]["concurrency_limited"] -= auth.CLIENT_AUDIT_EVERY_S + 1
        self.assertEqual(await self.post("hostile"), 429)
        lines = self.lines("concurrency_limited", "hostile")
        self.assertEqual((len(lines), lines[-1]["suppressed"]), (2, FLOOD - 1))
        self.gate.set()
        self.assertEqual(await asyncio.gather(*held), [200] * 4)
        self.assertEqual((stats["inflight"], await self.post("hostile")), (0, 200), "slots are free again")

    async def test_a_rate_limited_flood_writes_one_line(self):
        statuses = [await self.post("chatty", path="/api/generate") for _ in range(60)]
        self.assertEqual(statuses.count(200), 5)
        self.assertEqual(statuses.count(429), 55)
        self.assertEqual(state.CLIENT_STATS["clients"]["chatty"]["rate_limited"], 55)
        self.assertEqual(len(self.lines("rate_limited", "chatty")), 1)

    async def test_a_flood_for_a_role_it_may_not_use_writes_one_line(self):
        statuses = await asyncio.gather(*(self.post("narrow", path="/api/generate") for _ in range(100)))
        self.assertEqual(statuses, [403] * 100, "every request is still refused")
        self.assertEqual(state.CLIENT_STATS["clients"]["narrow"]["forbidden"], 100)
        self.assertEqual(len(self.lines("forbidden", "narrow")), 1)

    async def test_clients_and_event_kinds_are_throttled_apart(self):
        self.assertEqual(await self.post("chatty", path="/api/generate"), 200)
        held = [asyncio.create_task(self.post("hostile")) for _ in range(4)]
        await self.wait_inside("hostile", 4)
        await asyncio.gather(*(self.post("hostile") for _ in range(10)))
        for _ in range(10):
            await self.post("chatty", path="/api/generate")
        self.assertEqual((len(self.lines("concurrency_limited", "hostile")), len(self.lines("rate_limited", "chatty"))),
                         (1, 1), "one client's flood does not use up another client's audit line")
        self.gate.set()
        await asyncio.gather(*held)


if __name__ == "__main__":
    unittest.main(verbosity=1)
