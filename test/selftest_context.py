#!/usr/bin/env python3
"""Synthetic context projections and loopback inference; no external services."""
import asyncio
import copy
import hashlib
import json
import os
import sys
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import app, config, managed, nodes, ops, proxy, session_api, state  # noqa: E402
from skirnir_router.context_config import ContextConfig, parse_context_config  # noqa: E402
from skirnir_router.context_manager import compose, encoded, estimate, project, tool_delta  # noqa: E402
from skirnir_router.session_config import SessionConfig  # noqa: E402
from skirnir_router.outcome import conflict, failed  # noqa: E402
from skirnir_router.session_store import SessionStore  # noqa: E402
from skirnir_router.session_types import NOT_FOUND, UNAVAILABLE  # noqa: E402

BASE = os.path.join(os.path.dirname(__file__), "..", "router", "config.example.yaml")
PROJECT = "example-project"


def row(seq, role="user", content="Synthetic content", **extra):
    return {"seq": seq, "payload": {"role": role, "content": content, **extra}}


def history():
    return {"compaction": None, "checkpoint": None, "messages": [row(1, "system", "Synthetic instruction"),
            *[row(i, "user" if i % 2 == 0 else "assistant", "Synthetic conversation. " * 80) for i in range(2, 20)],
            row(20, content="Current question")]}


class ProjectionTests(unittest.TestCase):
    def test_configuration_optin_strict_and_store_dependency(self):
        self.assertFalse(parse_context_config(None).enabled)
        for raw in ([], {"enabled": 1}, {"tail_messages": True}, {"lease_s": 0}, {"reserve_tokens": "2"}, {"tokenizer": "auto"}):
            with self.subTest(raw=raw):
                self.assertTrue(failed(parse_context_config(raw)))
        raw = config.load_yaml(BASE)
        raw["router"]["context_manager"] = {"enabled": True}
        cfg = config.Config.__new__(config.Config)
        self.assertTrue(failed(cfg.parse(raw)))
        raw["router"]["session_store"] = {"enabled": True}
        self.assertIsNone(cfg.parse(raw))
        self.assertTrue(cfg.context_manager.enabled)

    def test_no_compaction_when_full_context_fits(self):
        context = history()
        messages, version = project(context, {}, 100000, ContextConfig(), "example-model")
        self.assertIsNone(version)
        self.assertEqual(messages, [r["payload"] for r in context["messages"]])

    def test_raw_immutable_system_tools_tail_and_checkpoint_preserved(self):
        context = history()
        context["messages"][3] = row(4, "assistant", "", tool_calls=[{"id": "call-a", "function": {"name": "example", "arguments": {}}}])
        context["messages"][4] = row(5, "tool", "Synthetic result", tool_call_id="call-a")
        context["messages"].insert(1, row(1, "developer", "Synthetic developer instruction"))
        context["checkpoint"] = {"version": 1, "payload": {"schema_version": 1, "goals": ["Synthetic goal"], "open_steps": ["Check output"]}}
        before = copy.deepcopy(context)
        cfg = ContextConfig(tail_messages=2)
        messages, version = project(context, {}, 6000, cfg, "example-model")
        self.assertEqual(context, before)
        self.assertLessEqual(estimate({}, messages), 6000)
        for index in (0, 1, 4, 5, -1):
            self.assertIn(context["messages"][index]["payload"], messages)
        self.assertTrue(any("Check output" in m.get("content", "") for m in messages))
        self.assertEqual(version["payload"]["algorithm"], "extractive-v1")
        self.assertGreater(version["through_seq"], 1)

    def test_smaller_model_creates_next_projection(self):
        context = history()
        cfg = ContextConfig(tail_messages=2)
        _, version = project(context, {}, 7000, cfg, "large-model")
        context["compaction"] = {**version, "version": 1}
        context["messages"] = [r for r in context["messages"] if r["seq"] > version["through_seq"]]
        messages, smaller = project(context, {}, 5000, cfg, "small-model")
        self.assertIsNotNone(smaller)
        self.assertLessEqual(estimate({}, messages), 5000)
        self.assertEqual(smaller["payload"]["provenance"]["previous_version"], 1)

    def test_protected_content_does_not_fit_fails_without_mutation(self):
        context = history()
        before = copy.deepcopy(context)
        self.assertEqual(project(context, {}, 200, ContextConfig(), "small-model").status, 413)
        self.assertEqual(context, before)

    def test_incomplete_long_current_turn_is_not_summarized(self):
        context = {"messages": [row(1, content="x" * 4000), *[row(i, "tool", "Synthetic result") for i in range(2, 12)]],
                   "compaction": None, "checkpoint": None}
        self.assertEqual(project(context, {}, 3000, ContextConfig(tail_messages=2), "small-model").status, 413)

    def test_failed_inputs_and_current_delta_remain_exact(self):
        context = history()
        context["protected_sequences"] = [2, 4]
        messages, version = project(context, {}, 11000, ContextConfig(tail_messages=2), "example")
        self.assertIsNotNone(version)
        for seq in (2, 4):
            self.assertIn(next(r["payload"] for r in context["messages"] if r["seq"] == seq), messages)
            self.assertIn(seq, [r["seq"] for r in version["payload"]["pinned"]])

    def test_ephemeral_memory_never_enters_compaction(self):
        context = history()
        memory = {"role": "user", "content": "UNTRUSTED_EPHEMERAL_RECALL"}
        messages, version = project(context, {}, 6000, ContextConfig(tail_messages=2), "example", memory)
        self.assertIn(memory, messages)
        self.assertNotIn("UNTRUSTED_EPHEMERAL_RECALL", encoded(version).decode())
        self.assertNotIn(memory, compose(context))

    def test_tools_schema_is_part_of_budget(self):
        context = history()
        self.assertEqual(project(context, {"tools": [{"description": "x" * 8000}]}, 6000, ContextConfig(tail_messages=2), "example").status, 413)

    def test_pending_tool_results_bound_to_persisted_calls(self):
        context = {"messages": [row(1, "assistant", "", tool_calls=[{"id": "call-a", "function": {"name": "example", "arguments": {}}}])],
                   "compaction": None, "checkpoint": None}
        result = tool_delta(context, [{"role": "tool", "content": "result", "tool_call_id": "call-a"}])
        self.assertEqual(result[0]["tool_name"], "example")
        for delta in ([{"role": "user", "content": "new"}], [{"role": "tool", "content": "result", "tool_call_id": "foreign"}]):
            with self.subTest(delta=delta):
                self.assertEqual(tool_delta(context, delta).status, 409)

    def test_request_and_capture_validation(self):
        for raw in (True, {"id": "bad", "mode": "skirnir-managed", "event_id": "x"},
                    {"id": str(uuid4()), "mode": "client-managed", "event_id": "x"}):
            with self.subTest(raw=raw):
                self.assertTrue(failed(managed.parse(raw)))
        for messages in ([], [{"role": "assistant", "content": "history"}], [{"role": "user", "content": "text", "images": ["synthetic"]}]):
            with self.subTest(messages=messages):
                self.assertTrue(failed(managed.validate_messages(messages)))
        capture = managed.AssistantCapture(1000)
        self.assertIsNone(capture.observe({"message": {"content": "a", "thinking": "b"}}))
        self.assertIsNone(capture.observe({"done": True, "message": {"content": "c"}}))
        self.assertEqual(capture.message, {"role": "assistant", "content": "ac", "thinking": "b"})
        self.assertEqual(capture.observe({"done": True}).status, 502)
        self.assertEqual(managed.AssistantCapture(10).observe({"message": {"content": "x" * 20}}).status, 502)
        self.assertEqual(managed.AssistantCapture(1000).observe(
            {"message": {"tool_calls": [{"function": {"name": "example", "arguments": "fragment"}}]}}).status, 502)
        # Lenient upstream JSON (NaN, unpaired surrogate) cannot be persisted: a Problem, not an encoder exception.
        for message in ({"content": "x", "extra": float("nan")}, {"content": "\ud800"}):
            self.assertEqual(managed.AssistantCapture(1000).observe({"message": message}).status, 502)


class ManagedHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = state.CFG, state.CLIENT_AUTH_MODE, state.SESSION
        state.CLIENT_AUTH_MODE = None
        cfg = config.Config(BASE, use_overrides=False)
        cfg.session_store = SessionConfig(enabled=True)
        cfg.context_manager = ContextConfig(enabled=True, tail_messages=2)
        cfg.client_auth = {"mode": "observe", "audit_log": None, "clients": {name: {
            "token_sha256": hashlib.sha256(name.encode()).hexdigest(), "session_projects": [PROJECT], "data_class": "personal"}
            for name in ("client-a", "client-b")}}
        state.CFG = cfg
        self.sid = uuid4()
        self.session = {"id": str(self.sid), "owner": "client-a", "project": PROJECT, "mode": "skirnir-managed", "status": "active", "data_class": "personal"}
        self.context = {"session": self.session, "messages": [], "compaction": None, "checkpoint": None}
        self.store = AsyncMock(spec=SessionStore)
        self.store.get.side_effect = self.get
        self.store.claim.side_effect = self.claim
        self.store.context.side_effect = lambda *args: copy.deepcopy(self.context)
        self.store.finish_turn.side_effect = self.finish
        self.store.compact.side_effect = self.compact
        self.store.compactions.return_value = []
        self.store.search.return_value = [row(1)]
        self.store.create.return_value = self.session
        upstream = web.Application()
        upstream.router.add_post("/api/chat", self.inference)
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
            patch.object(ops, "maybe_shadow"), patch.object(proxy.ausweich_beobachtung, "begin", return_value=None),
            patch.object(proxy.ausweich_beobachtung, "finish"), patch.object(proxy.poll, "poll_node", AsyncMock()),
            patch.object(session_api.PostgresSessionStore, "open", return_value=self.store),
            patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": "synthetic-test-only"})]
        for p in self.patches:
            p.start()
        self.mode, self.seen, self.completed = "normal", [], set()
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

    async def get(self, owner, sid):
        if owner != "client-a" or sid != self.sid:
            return NOT_FOUND
        return self.session

    async def claim(self, owner, sid, eid, fingerprint, messages, cfg):
        if eid in self.completed:
            return conflict("turn already completed")
        self.last_eid = eid
        for message in messages:
            self.context["messages"].append({"seq": len(self.context["messages"]) + 1, "payload": copy.deepcopy(message)})
        return {"lease": uuid4(), "input_end_seq": len(self.context["messages"])}

    async def finish(self, owner, sid, lease, message):
        self.context["messages"].append({"seq": len(self.context["messages"]) + 1, "payload": copy.deepcopy(message)})
        self.completed.add(self.last_eid)

    async def compact(self, owner, sid, lease, through, payload):
        return {"through_seq": through, "payload": payload, "version": 1}

    async def inference(self, request):
        data = await request.json()
        self.seen.append(data)
        message = {"role": "assistant", "content": "Synthetic answer"}
        if self.mode == "tool":
            message["tool_calls"] = [{"function": {"name": "example_tool", "arguments": {"value": 1}}}]
        if self.mode == "bad":
            return web.Response(text="invalid-json")
        if self.mode == "oversize":
            message["content"] = "x" * (state.CFG.context_manager.max_response_bytes + 1)
        if self.mode == "error":
            return web.json_response({"error": "Synthetic failure"}, status=400)
        if not data.get("stream"):
            return web.json_response({"message": message, "done": self.mode != "truncated"})
        chunks = [{"message": message, "done": False}]
        if self.mode != "truncated":
            chunks.append({"message": {"role": "assistant", "content": ""}, "done": True})
        if self.mode == "trailing-error":
            chunks.append({"error": "Synthetic failure after terminal"})
        return web.Response(body="".join(json.dumps(j) + "\n" for j in chunks), content_type="application/x-ndjson")

    async def infer(self, path="/api/chat", stream=False, eid="turn-1", token="client-a", extra=None):
        body = {"model": "example:latest", "messages": [{"role": "user", "content": "Synthetic question"}], "stream": stream,
                "routing": {"session": {"id": str(self.sid), "mode": "skirnir-managed", "event_id": eid}}}
        if path == "/v1/messages":
            body["max_tokens"] = 1024
        if extra:
            body.update(extra)
        return await self.client.post(path, json=body, headers={"Authorization": "Bearer " + token} if token else {})



class ManagedHttpTests(ManagedHarness):
    async def test_all_protocols_stream_and_unary_persist_before_success(self):
        for path in ("/api/chat", "/v1/chat/completions", "/v1/messages"):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    response = await self.infer(path, stream, eid=path + str(stream))
                    self.assertEqual(response.status, 200, await response.text())
                    data = await response.text()
                    self.assertNotIn('"error"', data)
                    self.assertEqual(self.context["messages"][-1]["payload"]["content"], "Synthetic answer")
                    self.assertEqual(self.seen[-1]["options"]["num_predict"], 1024)
        self.assertEqual(self.store.finish_turn.await_count, 6)
        ops.maybe_shadow.assert_not_called()
        proxy.ausweich_beobachtung.begin.assert_not_called()
        self.assertGreater(len(self.seen[-1]["messages"]), 1)

    async def test_isolation_optout_revocation_cloud_and_data_class(self):
        for token, expected in (("", 401), ("client-b", 404)):
            response = await self.infer(token=token)
            self.assertEqual(response.status, expected)
        state.CFG.context_manager = ContextConfig()
        self.assertEqual((await self.infer()).status, 404)
        state.CFG.context_manager = ContextConfig(enabled=True)
        response = await self.infer(extra={"routing": {"execution": "cloud", "session": {"id": str(self.sid), "mode": "skirnir-managed", "event_id": "a"}}})
        self.assertEqual(response.status, 403)
        self.session["data_class"] = "public"
        self.assertEqual((await self.infer()).status, 403)
        self.session["data_class"] = "personal"
        state.CFG.client_auth["clients"]["client-a"]["session_projects"] = []
        self.assertEqual((await self.infer()).status, 404)
        self.store.claim.assert_not_awaited()
        self.assertEqual(self.seen, [])

    async def test_invalid_delta_vision_and_nonchat_rejected_before_claim(self):
        for extra in ({"messages": []}, {"messages": [{"role": "assistant", "content": "history"}]},
                      {"messages": [{"role": "user", "content": "text", "images": ["synthetic"]}]}, {"template": "override"}):
            response = await self.infer(extra=extra)
            self.assertEqual(response.status, 400)
        self.assertEqual((await self.infer("/api/generate")).status, 400)
        response = await self.infer("/v1/chat/completions", extra={"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Synthetic question"},
            {"type": "image_url", "image_url": {"url": "https://images.example.invalid/example.png"}}]}]})
        self.assertEqual(response.status, 400)
        self.store.claim.assert_not_awaited()

    async def test_truncated_bad_oversized_and_error_responses_do_not_commit(self):
        for mode in ("truncated", "bad", "oversize", "error", "trailing-error"):
            for stream in (False, True):
                if mode == "trailing-error" and not stream:
                    continue
                self.mode = mode
                response = await self.infer(stream=stream, eid=mode + str(stream))
                data = await response.text()
                if stream and mode in ("truncated", "trailing-error", "oversize", "bad"):
                    self.assertIn('"error"', data)
                    self.assertNotIn('"done": true', data)
                else:
                    self.assertGreaterEqual(response.status, 400)
        self.store.finish_turn.assert_not_awaited()
        self.assertGreater(self.store.fail_turn.await_count, 0)

    async def test_database_failure_never_emits_success_terminal(self):
        self.store.finish_turn.side_effect = lambda *args, **kwargs: UNAVAILABLE
        for stream in (False, True):
            response = await self.infer(stream=stream, eid=str(stream))
            data = await response.text()
            self.assertIn("managed session unavailable", data)
            self.assertNotIn('"done": true', data)
            if not stream:
                self.assertEqual(response.status, 503)
        self.store.fail_turn.assert_awaited()

    async def test_duplicate_event_and_unscoped_cache_bypass(self):
        first = await self.infer()
        self.assertEqual(first.status, 200)
        response = await self.infer()
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.seen), 1)
        self.store.finish_turn.assert_awaited_once()

    async def test_compaction_persisted_before_forwarding_and_failure_is_closed(self):
        self.context.update(history())
        response = await self.infer()
        self.assertEqual(response.status, 200)
        self.store.compact.assert_awaited_once()
        self.assertTrue(any("Untrusted, lossy session summary" in m.get("content", "") for m in self.seen[-1]["messages"]))
        self.store.compact.side_effect = lambda *args, **kwargs: UNAVAILABLE
        before = len(self.seen)
        response = await self.infer(eid="second")
        self.assertEqual(response.status, 503)
        self.assertEqual(len(self.seen), before)

    async def test_snapshots_search_and_version_pagination_are_scoped(self):
        headers = {"Authorization": "Bearer client-a"}
        await self.infer()
        base = session_api.PREFIX + "/" + str(self.sid)
        response = await self.client.get(base + "/snapshot", headers=headers)
        snapshot = await response.json()
        self.assertFalse(snapshot["resumable_execution"])
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["messages"][-1]["role"], "assistant")
        response = await self.client.get(base + "/search?q=Synthetic&after=0&limit=2", headers=headers)
        self.assertEqual(response.status, 200)
        self.store.search.assert_awaited_once_with("client-a", self.sid, "Synthetic", 0, 2)
        self.assertEqual((await self.client.get(base + "/compactions?after=1", headers=headers)).status, 200)
        response = await self.client.get(base + "/snapshot", headers={"Authorization": "Bearer client-b"})
        self.assertEqual(response.status, 404)

    async def test_managed_create_is_explicit_and_optional(self):
        response = await self.client.post(session_api.PREFIX, headers={"Authorization": "Bearer client-a"},
                                          json={"project": PROJECT, "mode": "skirnir-managed"})
        self.assertEqual(response.status, 201)
        self.store.create.assert_awaited_once_with("client-a", PROJECT, "personal", {}, mode="skirnir-managed")

    async def test_tool_ids_survive_native_openai_anthropic_shapes(self):
        self.mode = "tool"
        for path in ("/api/chat", "/v1/chat/completions", "/v1/messages"):
            for stream in (False, True):
                response = await self.infer(path, stream, eid=path + str(stream))
                self.assertEqual(response.status, 200)
                data = await response.text()
                saved = self.context["messages"][-1]["payload"]["tool_calls"][0]
                self.assertIn(saved["id"], data)
                self.assertEqual(saved["function"]["arguments"], {"value": 1})

    async def test_target_budget_reserves_and_catalog_limit(self):
        self.tier["num_ctx"] = 4096
        state.CFG.models["example-model"] = {"context_tokens": 2048}
        response = await self.infer(extra={"options": {"num_predict": 1800}})
        self.assertEqual(response.status, 413)
        self.assertEqual(self.seen, [])
        self.assertEqual((await self.infer(eid="second", extra={"options": {"num_predict": -1}})).status, 200)
        self.assertEqual(self.seen[-1]["options"]["num_predict"], 1024)
        self.assertEqual(self.seen[-1]["options"]["num_ctx"], 2048)

    async def test_cancellation_and_lease_loss_preserve_raw_input(self):
        await self.infer()
        request = self.store.finish_turn.call_args
        self.assertIsNotNone(request)
        # Independently exercise heartbeat loss and cancellation cleanup.
        fake = {"client": "client-a"}
        turn = managed.Turn(fake, None, self.store, "client-a", self.sid, UUID(int=1), replace(ContextConfig(), lease_s=0.01))
        self.store.renew.return_value = UNAVAILABLE
        await turn.renew()
        self.assertTrue(turn.lost)
        self.assertEqual(turn.check().status, 409)
        turn.heartbeat = asyncio.create_task(asyncio.sleep(30))
        await turn.close()
        self.assertTrue(turn.heartbeat.cancelled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
