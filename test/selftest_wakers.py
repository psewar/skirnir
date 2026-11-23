#!/usr/bin/env python3
"""Self-test (Router 0.41.0, ADR-0117/ADR-0124): waking nodes through replaceable wakers.

Pins the built-in Wake-on-LAN waker (same magic packet, same repeat, same cooldown as before), checks the http_webhook
waker against a local receiver (accepted call, bearer token, missing token, redirect, refusal, timeout that does not block
other work, unreachable receiver), the configuration boundary (HTTPS outside literal loopback, no query, credentials or
redirect, unknown names refused) and the node policy. Runs without router, agent or network beyond loopback."""
import asyncio
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import yaml
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "router"))
from skirnir_router import config, nodes, registry, scheduler, state, wakers, wol  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402
from skirnir_router.waker_config import WebhookSpec, check_choice, parse_wakers  # noqa: E402

MAC = "aa:bb:cc:dd:ee:01"


def node(name="pc-example", **spec):
    n = nodes.Node(name, {"wol": True, "mac": MAC, **spec})
    n.state = "offline"
    n.last_known_models = {"model:8b"}
    return n


class FakeSocket:
    """Stands in for the UDP socket: records what would leave the router."""
    sent = []
    fail = False

    def __init__(self, family, kind):
        self.family, self.kind, self.options = family, kind, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def setsockopt(self, *args):
        self.options.append(args)

    def sendto(self, data, addr):
        if FakeSocket.fail:
            # exception-ok: stands in for the OSError a real socket raises when no route exists
            raise OSError("network unreachable")
        FakeSocket.sent.append((self.family, self.kind, tuple(self.options), data, addr))


class WakeOnLanPinned(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.real_socket = wol.socket.socket
        wol.socket.socket = FakeSocket
        FakeSocket.sent, FakeSocket.fail = [], False
        state.CFG = SimpleNamespace(wol_broadcasts=["198.51.100.255", "192.0.2.255"], wol_wait_s=90, wol_retry_s=20,
                                    wol_cooldown_s=300, wakers={})

    def tearDown(self):
        wol.socket.socket = self.real_socket

    def test_magic_packet_unchanged(self):
        wol.send_magic_packet("AA-BB-CC-DD-EE-01")
        packet = b"\xff" * 6 + bytes.fromhex("aabbccddee01") * 16
        self.assertEqual([(s[3], s[4]) for s in FakeSocket.sent],
                         [(packet, ("198.51.100.255", 9)), (packet, ("192.0.2.255", 9))])
        self.assertEqual(FakeSocket.sent[0][:3], (socket.AF_INET, socket.SOCK_DGRAM,
                                                  ((socket.SOL_SOCKET, socket.SO_BROADCAST, 1),)))

    async def test_wol_waker_facts(self):
        n = node()
        self.assertIs(wakers.waker_for(n), wakers.WOL)
        sent = await wakers.WOL.wake(n)
        self.assertEqual((sent.waker, sent.detail), ("wol", MAC))
        n.mac = "not-a-mac"
        problem = await wakers.WOL.wake(n)
        self.assertTrue(failed(problem))
        self.assertEqual(problem.kind, "wake_no_mac")
        n.mac = MAC
        FakeSocket.fail = True
        problem = await wakers.WOL.wake(n)
        self.assertEqual((problem.status, problem.kind), (503, "wake_send_failed"))

    def test_eligibility_as_before(self):
        """wol: policy wol and a MAC, like `n.wol and n.mac` before; a webhook needs no MAC; unknown names never wake."""
        state.CFG.wakers = {"plug": WebhookSpec(name="plug", url="https://automation.example.net/hook")}
        self.assertTrue(wakers.can_wake(node()))
        self.assertFalse(wakers.can_wake(node(mac=None)))
        self.assertFalse(wakers.can_wake(node(wol=False)))
        self.assertTrue(wakers.can_wake(node(mac=None, waker="plug")))
        self.assertFalse(wakers.can_wake(node(waker="plug", wol=False)))
        gone = node(waker="removed-later")
        self.assertFalse(wakers.can_wake(gone))
        self.assertEqual(wakers.waker_for(gone).kind, "wake_unknown_waker")
        state.NODES = {"a": node(mac=None), "b": node("b", mac=None, waker="plug")}
        self.assertEqual(scheduler.wakeable_for([{"model": "model:8b"}]).name, "b")


class FakeClock:
    """Replaces time and asyncio.sleep inside wakers only, so the wait loop runs in milliseconds."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


class WakeLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.saved = (wakers.time, wakers.asyncio, wakers.poll.poll_node, state.remember)
        wakers.time = SimpleNamespace(time=self.clock.time)
        wakers.asyncio = SimpleNamespace(sleep=self.clock.sleep, TimeoutError=asyncio.TimeoutError)
        self.events, self.calls = [], []
        state.remember = self.events.append
        state.CFG = SimpleNamespace(wol_broadcasts=["192.0.2.255"], wol_wait_s=30, wol_retry_s=6, wol_cooldown_s=300,
                                    wakers={})

    def tearDown(self):
        wakers.time, wakers.asyncio, wakers.poll.poll_node, state.remember = self.saved

    def fake_waker(self, repeat):
        test = self

        class Recorder:
            name, kind = "recorder", "http_webhook"

            def __init__(self):
                self.repeat = repeat

            def ready(self, n):
                return True

            async def wake(self, n):
                test.calls.append(test.clock.now)
                return wakers.Sent("recorder", "fixture")
        return Recorder()

    async def run_until_never_awake(self, waker):
        async def stays_offline(n):
            return None
        wakers.poll.poll_node = stays_offline
        real = wakers.waker_for
        wakers.waker_for = lambda n: waker
        try:
            return await wakers.wake(node())
        finally:
            wakers.waker_for = real

    async def test_repeat_follows_the_waker(self):
        start = self.clock.now
        self.assertFalse(await self.run_until_never_awake(self.fake_waker(repeat=True)))
        self.assertEqual([round(t - start) for t in self.calls], [0, 6, 12, 18, 24, 30], "WOL cadence as before: every retry_interval_s")
        self.calls.clear()
        self.clock.now += 1000
        self.assertFalse(await self.run_until_never_awake(self.fake_waker(repeat=False)))
        self.assertEqual(len(self.calls), 1, "a webhook without repeat is sent once per attempt (a toggle must not repeat)")

    async def test_awake_and_cooldown(self):
        n = node()
        polls = []

        async def comes_up(target):
            polls.append(target.name)
            if len(polls) == 2:
                target.state = "free"
        wakers.poll.poll_node = comes_up
        real_send = wol.send_magic_packet
        wol.send_magic_packet = lambda mac: self.calls.append(mac)
        try:
            self.assertTrue(await wakers.wake(n))
            self.assertEqual(self.calls, [MAC])
            self.assertEqual(self.events, [{"event": "wol", "node": "pc-example", "waker": "wol"}])
            n.state = "offline"
            self.assertFalse(await wakers.wake(n), "cooldown: no second attempt within cooldown_s")
            self.assertEqual(self.calls, [MAC])
        finally:
            wol.send_magic_packet = real_send

    async def test_failed_send_is_a_fact_not_a_wait(self):
        n = node(waker="missing")
        polled = []

        async def poll_node(target):
            polled.append(target)
        wakers.poll.poll_node = poll_node
        self.assertFalse(await wakers.wake(n))
        self.assertEqual((polled, self.events), ([], []), "nothing sent: no event, no waiting")


class Receiver:
    def __init__(self, status=204, delay=0.0, location=None):
        self.status, self.delay, self.location, self.hits = status, delay, location, []

    async def handle(self, request):
        self.hits.append({"method": request.method, "path": request.path, "auth": request.headers.get("Authorization"),
                          "type": request.headers.get("Content-Type", ""), "body": await request.json()})
        if self.delay:
            await asyncio.sleep(self.delay)
        headers = {"Location": self.location} if self.location else {}
        return web.Response(status=self.status, headers=headers)


class Webhook(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = aiohttp.ClientSession()
        state.SESSION = self.session
        os.environ.pop("SKIRNIR_TEST_WAKE_TOKEN", None)

    async def asyncTearDown(self):
        await self.session.close()
        os.environ.pop("SKIRNIR_TEST_WAKE_TOKEN", None)

    async def serve(self, receiver):
        app = web.Application()
        app.router.add_route("*", "/{rest:.*}", receiver.handle)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        self.addAsyncCleanup(server.close)
        return server

    def waker(self, server, **kw):
        return wakers.WebhookWaker(WebhookSpec(name="plug", url=str(server.make_url("/api/webhook/wake")), **kw))

    async def test_accepted_with_fixed_body_and_token(self):
        rx = Receiver()
        server = await self.serve(rx)
        os.environ["SKIRNIR_TEST_WAKE_TOKEN"] = "synthetic-token"
        sent = await self.waker(server, token_env="SKIRNIR_TEST_WAKE_TOKEN").wake(node())
        self.assertFalse(failed(sent), getattr(sent, "message", ""))
        self.assertEqual(sent.waker, "plug")
        self.assertNotIn("/api/webhook", sent.detail, "the log line never carries the URL path (it may hold a secret)")
        self.assertEqual(rx.hits, [{"method": "POST", "path": "/api/webhook/wake", "auth": "Bearer synthetic-token",
                                    "type": "application/json", "body": {"event": "wake", "node": "pc-example", "mac": MAC}}])

    async def test_without_token_no_header(self):
        rx = Receiver(status=200)
        server = await self.serve(rx)
        sent = await self.waker(server).wake(node(mac=None))
        self.assertFalse(failed(sent))
        self.assertEqual((rx.hits[0]["auth"], rx.hits[0]["body"]["mac"]), (None, None))

    async def test_missing_token_fails_closed(self):
        rx = Receiver()
        server = await self.serve(rx)
        problem = await self.waker(server, token_env="SKIRNIR_TEST_WAKE_TOKEN").wake(node())
        self.assertEqual((problem.status, problem.kind), (503, "wake_secret_missing"))
        self.assertEqual(rx.hits, [], "a missing secret never becomes an unauthenticated call")

    async def test_redirect_is_not_followed(self):
        second = Receiver()
        target = await self.serve(second)
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                first = Receiver(status=status, location=str(target.make_url("/elsewhere")))
                server = await self.serve(first)
                problem = await self.waker(server).wake(node())
                self.assertEqual((problem.status, problem.kind), (502, "wake_redirect"))
                self.assertEqual(len(first.hits), 1)
        self.assertEqual(second.hits, [], "the redirect target is never contacted")

    async def test_refusal(self):
        server = await self.serve(Receiver(status=500))
        problem = await self.waker(server).wake(node())
        self.assertEqual((problem.status, problem.kind), (502, "wake_refused"))

    async def test_timeout_does_not_block_other_work(self):
        server = await self.serve(Receiver(delay=3))
        ticks = []

        async def other_work():
            for _ in range(5):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)
        t0 = time.monotonic()
        problem, _ = await asyncio.gather(self.waker(server, timeout_s=0.5).wake(node()), other_work())
        elapsed = time.monotonic() - t0
        self.assertEqual((problem.status, problem.kind), (503, "wake_unreachable"))
        self.assertLess(elapsed, 2.5, "the configured timeout ends the call")
        self.assertEqual(len(ticks), 5)
        self.assertLess(ticks[-1] - t0, 0.5, "other coroutines keep running while the webhook waits")

    async def test_unreachable(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()   # nothing listens on this port any more
        w = wakers.WebhookWaker(WebhookSpec(name="plug", url=f"http://127.0.0.1:{port}/hook", timeout_s=2))
        problem = await w.wake(node())
        self.assertEqual(problem.kind, "wake_unreachable")


class Configuration(unittest.TestCase):
    def test_parse_accepted(self):
        got = parse_wakers({"plug": {"kind": "http_webhook", "url": "https://automation.example.net/api/webhook/x",
                                     "token_env": "WAKE_TOKEN", "timeout_s": 5, "repeat": True},
                            "local": {"kind": "http_webhook", "url": "http://127.0.0.1:8123/hook"},
                            "local6": {"kind": "http_webhook", "url": "http://[::1]:8123/hook"}})
        self.assertFalse(failed(got), getattr(got, "message", ""))
        self.assertEqual((got["plug"].timeout_s, got["plug"].repeat, got["local"].timeout_s, got["local"].repeat),
                         (5.0, True, 10.0, False))
        self.assertEqual(parse_wakers(None), {})

    def test_parse_rejected(self):
        ok = {"kind": "http_webhook", "url": "https://automation.example.net/hook"}
        for label, raw in [
            ("plain HTTP to a non-loopback host", {"x": {**ok, "url": "http://automation.example.net/hook"}}),
            ("localhost is not literal loopback", {"x": {**ok, "url": "http://localhost:8123/hook"}}),
            ("query", {"x": {**ok, "url": "https://automation.example.net/hook?turn=on"}}),
            ("fragment", {"x": {**ok, "url": "https://automation.example.net/hook#x"}}),
            ("credentials in the URL", {"x": {**ok, "url": "https://user@automation.example.test/hook"}}),
            ("other scheme", {"x": {**ok, "url": "ftp://127.0.0.1/hook"}}),
            ("invalid port", {"x": {**ok, "url": "https://automation.example.net:port/hook"}}),
            ("no URL", {"x": {"kind": "http_webhook"}}),
            ("command waker does not exist", {"x": {**ok, "kind": "command"}}),
            ("ipmi not implemented", {"x": {**ok, "kind": "ipmi"}}),
            ("built-in name", {"wol": ok}),
            ("bad name", {"has space": ok}),
            ("unknown key", {"x": {**ok, "command": "/bin/true"}}),
            ("token in the file instead of an env name", {"x": {**ok, "token_env": "secret value"}}),
            ("timeout too long", {"x": {**ok, "timeout_s": 600}}),
            ("timeout not a number", {"x": {**ok, "timeout_s": "10"}}),
            ("repeat not boolean", {"x": {**ok, "repeat": "yes"}}),
            ("not a mapping", ["x"]),
        ]:
            with self.subTest(label):
                self.assertTrue(failed(parse_wakers(raw)), label)

    def test_choice(self):
        known = {"plug": object()}
        for choice in (None, "wol", "plug"):
            self.assertIsNone(check_choice(choice, known, "waker"))
        for choice in ("unknown", 3, ""):
            self.assertTrue(failed(check_choice(choice, known, "waker")))

    def test_config_load(self):
        data = yaml.safe_load((ROOT / "router/config.example.yaml").read_text(encoding="utf-8"))
        data["router"]["wakers"] = {"plug": {"kind": "http_webhook", "url": "https://automation.example.net/hook"}}
        cases = [({"pc-example": {"wol": True, "waker": "plug"}}, True), ({"pc-example": {"wol": True, "waker": "wol"}}, True),
                 ({"pc-example": {"wol": True}}, True), ({"pc-example": {"wol": True, "waker": "missing"}}, False)]
        with tempfile.TemporaryDirectory() as td:
            for n, (nodes_cfg, valid) in enumerate(cases):
                with self.subTest(nodes=nodes_cfg):
                    data["nodes"] = nodes_cfg
                    path = Path(td) / f"{n}.yaml"
                    path.write_text(yaml.safe_dump(data), encoding="utf-8")
                    result = config.Config.load(str(path), use_overrides=False)
                    self.assertEqual(not failed(result), valid, getattr(result, "message", ""))
            data["nodes"] = {}
            data["router"]["wakers"]["plug"]["url"] = "http://automation.example.net/hook"
            path = Path(td) / "http.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            self.assertTrue(failed(config.Config.load(str(path), use_overrides=False)))

    def test_node_spec_and_policy(self):
        state.CFG = SimpleNamespace(nodes={"pc-example": {"waker": "plug"}},
                                    wakers={"plug": object(), "other": object()})
        spec = registry.node_spec_from_entry({"name": "pc-example", "facts": {}, "policy": {}})
        self.assertEqual(spec["waker"], "plug", "nodes.<name>.waker is the base for a registered node")
        spec = registry.node_spec_from_entry({"name": "pc-example", "facts": {}, "policy": {"waker": "other"}})
        self.assertEqual(spec["waker"], "other", "the policy overrides it")
        spec = registry.node_spec_from_entry({"name": "new", "facts": {}, "policy": {}})
        self.assertEqual(spec["waker"], "wol", "default: Wake-on-LAN")
        self.assertEqual(registry.node_policy({"waker": "other"}), {"waker": "other"})
        self.assertEqual(registry.node_policy({"waker": None}), {"waker": None})
        problem = registry.node_policy({"waker": "https://elsewhere.example.net/hook"})
        self.assertEqual(problem.status, 400, "the UI picks a configured name, never a destination")
        n = nodes.Node("pc-example", {"wol": True})
        self.assertEqual(n.waker, "wol")
        n.apply_spec({"waker": "plug"})
        self.assertEqual(n.waker, "plug")


if __name__ == "__main__":
    unittest.main(verbosity=1)
