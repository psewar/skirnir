#!/usr/bin/env python3
"""Self-test response shapes (ADR-0120): the Shape defaults, the facts each format reports, and that the native Ollama
format reaches the client byte for byte as before the relay called a shape for it.

Router 0.37.2 replaced the relay's `hasattr(self.shape, ...)` and `shape is None` branches with a Shape base class and
`OllamaShape`. The wire tests run the router app in-process against a synthetic Ollama upstream on loopback and compare
the exact bytes a native client receives (unary, stream, unparsable lines, Ollama's own error line, a node's 4xx) with
literals; no router, network or agent needed."""
import hashlib
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import anthropic_api, app, config, nodes, ops, proxy, state  # noqa: E402
from skirnir_router import openai_api  # noqa: E402
from skirnir_router.shape import OllamaShape, Shape  # noqa: E402

BASE = os.path.join(os.path.dirname(__file__), "..", "router", "config.example.yaml")


class Minimal(Shape):
    """Only what a format must define; everything else comes from the defaults."""
    name = "minimal"
    error = staticmethod(lambda status, msg, why=None: web.json_response({"minimal": msg}, status=status))

    def complete(self, j):
        return b"C"

    def chunk(self, j):
        return b"c"


class ShapeDefaultsTests(unittest.TestCase):
    def test_defaults_write_nothing_extra(self):
        s = Minimal()
        self.assertIsNone(s.bind("req-1", "example:latest"))
        self.assertEqual((s.head(), s.ping(), s.tail(), s.stream_error(502, "x")), (b"", b"", b"", b""))
        self.assertEqual(s.keepalive_s, 0)
        self.assertEqual(s.stream_content_type, "application/x-ndjson")
        self.assertFalse(s.verbatim)
        self.assertFalse(s.reports_abort_in_stream)
        self.assertEqual(s.raw_line(b"not json\n"), b"", "a foreign format drops unparsable lines")
        self.assertEqual(s.upstream_error_line(b'{"error": "x"}\n', 502, "x"), b"", "no error event: nothing")

    def test_default_errors_use_the_format(self):
        s = Minimal()
        r = s.client_error(400, "synthetic", {"X-Skirnir-Node": "n"})
        self.assertEqual((r.status, json.loads(r.body)), (400, {"minimal": "synthetic"}))
        r = s.pass_through(400, b'{"error": "synthetic refusal"}', {})
        self.assertEqual(json.loads(r.body), {"minimal": "synthetic refusal"}, "the node's error message, in the format")
        r = s.pass_through(501, b"plain text", {})
        self.assertEqual((r.status, json.loads(r.body)), (501, {"minimal": "plain text"}))

    def test_required_methods_are_enforced(self):
        class Incomplete(Shape):
            name = "incomplete"

            def complete(self, j):
                return b""
        # a missing required method is a programming error: the class cannot be instantiated (genuine exception)
        with self.assertRaises(TypeError):
            Incomplete()

    def test_formats_report_their_facts(self):
        chat, completion, embed = openai_api.OpenAIChatShape(), openai_api.OpenAICompletionShape(), openai_api.OpenAIEmbedShape()
        anthropic, native = anthropic_api.AnthropicShape("example:latest", 7), OllamaShape()
        for s in (chat, completion, embed, anthropic, native):
            self.assertIsInstance(s, Shape)
        self.assertEqual([s.name for s in (chat, completion, embed, anthropic, native)], ["openai", "openai", "openai", "anthropic", "ollama"])
        self.assertEqual([s.reports_abort_in_stream for s in (chat, completion, embed, anthropic, native)], [True, True, False, True, False])
        self.assertEqual([s.verbatim for s in (chat, completion, embed, anthropic, native)], [False, False, False, False, True])
        self.assertEqual([s.keepalive_s for s in (chat, embed, anthropic, native)], [0, 0, 7, 0])
        self.assertEqual(embed.stream_error(502, "x"), b"", "embeddings have no stream error event, as before")
        self.assertTrue(anthropic.head().startswith(b"event: message_start"))
        self.assertEqual(chat.tail(), b"data: [DONE]\n\n")

    def test_native_shape_matches_the_previous_expressions(self):
        s, j = OllamaShape(), {"model": "example:latest", "message": {"role": "assistant", "content": "café"}, "done": True}
        self.assertEqual(s.complete(j), json.dumps(j).encode())
        self.assertEqual(s.chunk(j), (json.dumps(j) + "\n").encode())
        self.assertEqual(s.stream_error(502, "upstream n aborted"), b'{"error": "upstream n aborted"}\n')
        self.assertEqual(s.upstream_error_line(b'{"error": "x"}', 502, "y"), b'{"error": "x"}\n', "newline appended")
        self.assertEqual(s.upstream_error_line(b'{"error": "x"}\n', 502, "y"), b'{"error": "x"}\n', "line verbatim")
        self.assertEqual(s.raw_line(b"not json\n"), b"not json\n")
        self.assertEqual((s.head(), s.tail(), s.stream_content_type), (b"", b"", "application/x-ndjson"))
        r = s.error(403, "memory policy changed")
        self.assertEqual((r.status, json.loads(r.body)), (403, {"error": "memory policy changed"}))
        r = s.client_error(400, "synthetic", {"X-Skirnir-Node": "n"})
        self.assertEqual((r.status, r.body, r.headers["X-Skirnir-Node"]), (400, b'{"error": "synthetic"}', "n"))
        r = s.pass_through(400, b'{"error":"raw"}', {"X-Skirnir-Node": "n"})
        self.assertEqual((r.status, r.body, r.content_type, r.headers["X-Skirnir-Node"]), (400, b'{"error":"raw"}', "application/json", "n"))


# --- wire: the router app against a synthetic Ollama upstream --------------------------------------------------------

CHUNK1 = b'{"model": "example-model", "message": {"role": "assistant", "content": "Hel"}, "done": false}\n'
RAW = b"synthetic line that is not json\n"
ERRLINE = b'{"error": "synthetic parser failure"}\n'
CHUNK2 = b'{"model": "example-model", "message": {"role": "assistant", "content": "lo"}, "done": false}\n'
DONE = b'{"model": "example-model", "message": {"role": "assistant", "content": ""}, "done": true, "done_reason": "stop"}\n'
UNARY = b'{"model": "example-model", "message": {"role": "assistant", "content": "Hello"}, "done": true}'
GENERATE = (b'{"model": "example-model", "response": "Hi", "done": false}\n'
            b'{"model": "example-model", "response": "", "done": true}\n')


class WireHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = state.CFG, state.CLIENT_AUTH_MODE, state.SESSION
        state.CLIENT_AUTH_MODE = None
        cfg = config.Config(BASE, use_overrides=False)
        cfg.client_auth = {"mode": "observe", "audit_log": None, "clients": {"client-a": {
            "token_sha256": hashlib.sha256(b"client-a").hexdigest(), "data_class": "personal", "models": True}}}
        state.CFG = cfg
        self.mode = "stream"
        upstream = web.Application()
        upstream.router.add_post("/api/chat", self.inference)
        upstream.router.add_post("/api/generate", self.inference)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        self.node = nodes.Node("synthetic-worker", {"ollama": str(self.upstream.make_url("/"))})
        self.node.state = "free"
        self.node.loaded = {"example-model": 1}
        self.tier = {"model": "example-model", "num_ctx": 8192, "busy_ok": False}
        self.role = {"name": "example", "exposed": "example:latest", "tiers": [self.tier]}
        cfg.roles = {"example": self.role}
        self.patches = [patch.object(proxy.scheduler, "resolve_tiers", return_value=(self.role, [self.tier])),
                        patch.object(proxy._Acquire, "next_candidate", AsyncMock(return_value=(0, self.tier, 8192, self.node))),
                        patch.object(ops, "maybe_shadow"), patch.object(proxy.fallback_observation, "begin", return_value=None),
                        patch.object(proxy.fallback_observation, "finish"), patch.object(proxy.poll, "poll_node", AsyncMock())]
        for p in self.patches:
            p.start()
        state.SESSION = ClientSession()
        api, _ = app.build_apps()
        self.client = TestClient(TestServer(api))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await state.SESSION.close()
        await self.upstream.close()
        for p in reversed(self.patches):
            p.stop()
        state.CFG, state.CLIENT_AUTH_MODE, state.SESSION = self.previous

    async def inference(self, request):
        if request.path == "/api/generate":
            return web.Response(body=GENERATE, content_type="application/x-ndjson")
        if self.mode == "stream":
            return web.Response(body=CHUNK1 + RAW + ERRLINE + CHUNK2 + DONE, content_type="application/x-ndjson")
        if self.mode == "unary":
            return web.Response(body=UNARY, content_type="application/json")
        if self.mode == "invalid":
            return web.Response(body=b"synthetic-not-json", content_type="application/json")
        return web.Response(status=400, body=b'{"error":"synthetic bad request"}', content_type="application/json")

    async def post(self, path, stream, extra=None):
        body = {"model": "example:latest", "stream": stream, **(extra or {})}
        if path == "/api/generate":
            body["prompt"] = "Synthetic question"
        else:
            body["messages"] = [{"role": "user", "content": "Synthetic question"}]
        if path == "/v1/messages":
            body["max_tokens"] = 256
        return await self.client.post(path, json=body, headers={"Authorization": "Bearer client-a"})


class NativeWireTests(WireHarness):
    async def test_native_stream_is_byte_identical(self):
        r = await self.post("/api/chat", True)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "application/x-ndjson")
        self.assertEqual(r.headers["X-Skirnir-Node"], "synthetic-worker")
        expected = (CHUNK1.replace(b"example-model", b"example:latest") + RAW + ERRLINE
                    + CHUNK2.replace(b"example-model", b"example:latest") + DONE.replace(b"example-model", b"example:latest"))
        self.assertEqual(await r.read(), expected, "model renamed, unparsable and error lines verbatim, no head or tail")

    async def test_native_generate_stream_is_byte_identical(self):
        r = await self.post("/api/generate", True)
        self.assertEqual(await r.read(), GENERATE.replace(b"example-model", b"example:latest"))

    async def test_native_unary_is_byte_identical(self):
        self.mode = "unary"
        r = await self.post("/api/chat", False)
        self.assertEqual((r.status, r.headers["Content-Type"]), (200, "application/json"))
        self.assertEqual(await r.read(), UNARY.replace(b"example-model", b"example:latest"))

    async def test_native_unparsable_unary_is_forwarded_verbatim(self):
        self.mode = "invalid"
        r = await self.post("/api/chat", False)
        self.assertEqual((r.status, await r.read()), (200, b"synthetic-not-json"))

    async def test_native_node_refusal_passes_through_raw(self):
        self.mode = "refuse"
        r = await self.post("/api/chat", False)
        self.assertEqual((r.status, await r.read()), (400, b'{"error":"synthetic bad request"}'))
        self.assertEqual(r.headers["X-Skirnir-Node"], "synthetic-worker", "routing headers on a passed-through error")


class ForeignWireTests(WireHarness):
    async def test_openai_stream_drops_raw_lines_and_ends_with_done(self):
        r = await self.post("/v1/chat/completions", True)
        data = await r.read()
        self.assertEqual(r.headers["Content-Type"], "text/event-stream")
        self.assertNotIn(b"synthetic line that is not json", data)
        self.assertIn(b'"code": "upstream_error"', data, "Ollama's error line becomes an OpenAI error event")
        self.assertTrue(data.endswith(b"data: [DONE]\n\n"))

    async def test_openai_refusal_in_openai_format(self):
        self.mode = "refuse"
        r = await self.post("/v1/chat/completions", False)
        self.assertEqual(r.status, 400)
        self.assertEqual((await r.json())["error"]["message"], "synthetic bad request")

    async def test_anthropic_stream_has_head_and_error_event(self):
        r = await self.post("/v1/messages", True)
        data = await r.read()
        self.assertTrue(data.startswith(b"event: message_start"))
        self.assertIn(b"event: error", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
