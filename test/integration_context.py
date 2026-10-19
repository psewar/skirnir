#!/usr/bin/env python3
"""Real disposable PostgreSQL tests for leases, compactions and inference lifecycle."""
import asyncio
import copy
import hashlib
import os
import unittest
from dataclasses import replace
from uuid import UUID, uuid4

from selftest_context import ManagedHarness, PROJECT, encoded, history
from skirnir_router import session_api
from skirnir_router.context_config import ContextConfig
from skirnir_router.context_manager import compose, estimate, project
from skirnir_router.session_config import SessionConfig
from skirnir_router.session_store import PostgresSessionStore
from skirnir_router.session_types import NOT_FOUND

DSN = os.environ.get("SKIRNIR_TEST_SESSION_DSN")


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class ManagedPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        self.owner = "test-" + uuid4().hex
        self.other = "test-" + uuid4().hex
        self.session = await self.store.create(self.owner, PROJECT, "personal", {}, mode="skirnir-managed")
        self.sid = UUID(self.session["id"])
        self.cfg = ContextConfig(enabled=True, tail_messages=2)

    async def asyncTearDown(self):
        try:
            async with self.store.pool.acquire() as conn:
                await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=ANY($1::text[])", [self.owner, self.other])
        finally:
            await self.store.close()

    async def claim(self, eid="turn-1", messages=None):
        messages = messages or [{"role": "user", "content": "Synthetic question"}]
        return await self.store.claim(self.owner, self.sid, eid, hashlib.sha256(encoded(messages)).hexdigest(), messages, self.cfg)

    async def test_serialized_claims_idempotent_failure_retry_and_completed_conflict(self):
        results = await asyncio.gather(*(self.claim() for _ in range(5)))
        successful = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(successful), 1)
        self.assertTrue(all(isinstance(r, dict) or r.status == 409 for r in results))
        lease = successful[0]["lease"]
        await self.store.fail_turn(self.owner, self.sid, lease)
        retried = await self.claim()
        self.assertNotEqual(retried["lease"], lease)
        self.assertEqual(len(await self.store.read(self.owner, self.sid, "message", 0, 100)), 1)
        self.assertEqual((await self.claim(messages=[{"role": "user", "content": "Changed synthetic question"}])).status, 409)
        await self.store.finish_turn(self.owner, self.sid, retried["lease"], {"role": "assistant", "content": "Synthetic answer"})
        self.assertEqual((await self.claim()).status, 409)
        self.assertEqual(len(await self.store.read(self.owner, self.sid, "message", 0, 100)), 2)

    async def test_expired_worker_cannot_commit_or_renew_new_lease(self):
        turn = await self.claim()
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.turns SET lease_until=clock_timestamp()-interval '1 second' WHERE owner=$1 AND session_id=$2", self.owner, self.sid)
        replacement = await self.claim()
        for action in (self.store.renew(self.owner, self.sid, turn["lease"], 90),
                       self.store.finish_turn(self.owner, self.sid, turn["lease"], {"role": "assistant", "content": "stale"})):
            self.assertEqual((await action).status, 409)
        await self.store.fail_turn(self.owner, self.sid, turn["lease"])
        await self.store.renew(self.owner, self.sid, replacement["lease"], 90)
        await self.store.finish_turn(self.owner, self.sid, replacement["lease"], {"role": "assistant", "content": "current"})
        self.assertEqual((await self.store.read(self.owner, self.sid, "message", 0, 100))[-1]["payload"]["content"], "current")

    async def test_older_failed_turn_cannot_replay_after_new_input(self):
        first = await self.claim()
        await self.store.fail_turn(self.owner, self.sid, first["lease"])
        next_turn = await self.claim(eid="turn-2")
        await self.store.fail_turn(self.owner, self.sid, next_turn["lease"])
        context = await self.store.context(self.owner, self.sid, self.cfg)
        self.assertEqual(context["protected_sequences"], [1, 2])
        self.assertEqual((await self.claim()).status, 409)
        self.assertEqual((await self.store.append(self.owner, self.sid, "manual", "message", {"role": "user", "content": "manual"})).status, 409)

    async def test_compaction_versions_do_not_modify_raw_and_survive_reopen(self):
        raw = history()["messages"]
        # Feed synthetic historical pairs through actual turn transactions.
        for index in range(0, len(raw) - 1, 2):
            delta = [{"role": "user", "content": raw[index]["payload"]["content"]}]
            turn = await self.claim(eid=str(index), messages=delta)
            await self.store.finish_turn(self.owner, self.sid, turn["lease"], {"role": "assistant", "content": raw[index + 1]["payload"]["content"]})
        turn = await self.claim(eid="current")
        before = await self.store.read(self.owner, self.sid, "message", 0, 100)
        context = await self.store.context(self.owner, self.sid, self.cfg)
        _, projection = project(context, {}, 7000, self.cfg, "large-model")
        first = await self.store.compact(self.owner, self.sid, turn["lease"], projection["through_seq"], projection["payload"])
        context = await self.store.context(self.owner, self.sid, self.cfg)
        _, smaller = project(context, {}, estimate({}, compose(context)) - 512, self.cfg, "small-model")
        second = await self.store.compact(self.owner, self.sid, turn["lease"], smaller["through_seq"], smaller["payload"])
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(before, await self.store.read(self.owner, self.sid, "message", 0, 100))
        await self.store.close()
        self.store = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        self.assertEqual((await self.store.context(self.owner, self.sid, self.cfg))["compaction"], second)
        self.assertEqual(await self.store.compactions(self.owner, self.sid, 0, 1), [first])
        self.assertEqual(await self.store.compactions(self.owner, self.sid, 1, 10), [second])

    async def test_checkpoint_during_turn_is_versioned_and_status_write_blocked(self):
        turn = await self.claim()
        self.assertEqual((await self.store.update(self.owner, self.sid, "archived", None)).status, 409)
        first, _ = await self.store.append(self.owner, self.sid, "checkpoint-1", "checkpoint", {"schema_version": 1, "open_steps": ["Synthetic pending step"]})
        second, _ = await self.store.append(self.owner, self.sid, "checkpoint-2", "checkpoint", {"schema_version": 1, "open_steps": ["Synthetic next step"]})
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual((await self.store.context(self.owner, self.sid, self.cfg))["checkpoint"]["payload"], second["payload"])
        await self.store.finish_turn(self.owner, self.sid, turn["lease"], {"role": "assistant", "content": "Synthetic answer"})
        messages = await self.store.read(self.owner, self.sid, "message", 0, 100)
        self.assertEqual([m["seq"] for m in messages], [1, 4])

    async def test_scope_search_delete_cascade_and_no_resurrection(self):
        turn = await self.claim()
        for action in (self.store.context(self.other, self.sid, self.cfg), self.store.compactions(self.other, self.sid, 0, 10),
                       self.store.search(self.other, self.sid, "Synthetic", 0, 10), self.store.claim(self.other, self.sid, "x", "x", [], self.cfg)):
            self.assertEqual(await action, NOT_FOUND)
        self.assertEqual(len(await self.store.search(self.owner, self.sid, "synthetic", 0, 10)), 1)
        self.assertEqual(await self.store.search(self.owner, self.sid, "%' OR 1=1 --", 0, 10), [])
        await self.store.compact(self.owner, self.sid, turn["lease"], 0, {"summary": "Synthetic", "pinned": []})
        await self.store.delete(self.owner, self.sid)
        self.assertEqual(await self.store.finish_turn(self.owner, self.sid, turn["lease"], {"role": "assistant", "content": "late"}), NOT_FOUND)
        await self.store.fail_turn(self.owner, self.sid, turn["lease"])
        async with self.store.pool.acquire() as conn:
            for table in ("events", "turns", "compactions"):
                self.assertEqual(await conn.fetchval(f"SELECT count(*) FROM skirnir_sessions.{table} WHERE owner=$1 AND session_id=$2", self.owner, self.sid), 0)

    async def test_history_limit_rolls_back_claim_and_inputs(self):
        self.cfg = replace(self.cfg, max_history_bytes=4096)
        self.assertEqual((await self.claim(messages=[{"role": "user", "content": "x" * 5000}])).status, 413)
        self.assertEqual(await self.store.read(self.owner, self.sid, "message", 0, 100), [])
        await self.claim()

    async def test_delta_tool_pair_resolution_and_orphan_rollback(self):
        turn = await self.claim()
        call = {"id": "call-example", "function": {"name": "example_tool", "arguments": {}}}
        await self.store.finish_turn(self.owner, self.sid, turn["lease"], {"role": "assistant", "content": "", "tool_calls": [call]})
        before = await self.store.read(self.owner, self.sid, "message", 0, 100)
        self.assertEqual((await self.claim(eid="orphan", messages=[{"role": "tool", "content": "result", "tool_call_id": "foreign"}])).status, 409)
        self.assertEqual(before, await self.store.read(self.owner, self.sid, "message", 0, 100))
        await self.claim(eid="tool-result", messages=[{"role": "tool", "content": "Synthetic result", "tool_call_id": "call-example"}])
        result = (await self.store.read(self.owner, self.sid, "message", 0, 100))[-1]["payload"]
        self.assertEqual(result["tool_name"], "example_tool")


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class ManagedInferencePostgresTests(ManagedHarness):
    async def asyncSetUp(self):
        # Open before the harness installs its mock startup adapter.
        self.real = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        await super().asyncSetUp()
        self.session = await self.real.create("client-a", PROJECT, "personal", {}, mode="skirnir-managed")
        self.sid = UUID(self.session["id"])
        self.client.server.app[session_api.STORE_KEY] = self.real

    async def asyncTearDown(self):
        await self.real.delete("client-a", self.sid)   # NOT_FOUND when the test deleted it already
        await super().asyncTearDown()
        await self.real.close()

    async def test_real_all_protocols_stream_and_unary_then_snapshot(self):
        for path in ("/api/chat", "/v1/chat/completions", "/v1/messages"):
            for stream in (False, True):
                response = await self.infer(path, stream, eid=path + str(stream))
                self.assertEqual(response.status, 200, await response.text())
                self.assertNotIn('"error"', await response.text())
        messages = await self.real.read("client-a", self.sid, "message", 0, 100)
        self.assertEqual(len(messages), 12)
        self.assertEqual(messages[-1]["payload"]["content"], "Synthetic answer")
        response = await self.client.get(session_api.PREFIX + "/" + str(self.sid) + "/snapshot", headers={"Authorization": "Bearer client-a"})
        snapshot = await response.json()
        self.assertEqual(snapshot["provenance"]["message_head"], 12)
        self.assertFalse(snapshot["resumable_execution"])

    async def test_real_truncated_stream_retry_deduplicates_raw_input(self):
        self.mode = "truncated"
        response = await self.infer(stream=True)
        self.assertIn('"error"', await response.text())
        self.assertEqual(len(await self.real.read("client-a", self.sid, "message", 0, 100)), 1)
        self.mode = "normal"
        response = await self.infer(stream=True)
        self.assertNotIn('"error"', await response.text())
        self.assertEqual(len(await self.real.read("client-a", self.sid, "message", 0, 100)), 2)
        self.assertEqual((await self.infer()).status, 409)

    async def test_real_tool_roundtrip_delta_only_across_protocols(self):
        for path in ("/api/chat", "/v1/chat/completions", "/v1/messages"):
            self.mode = "tool"
            response = await self.infer(path, stream=True, eid=path + "-tool")
            output = await response.text()
            messages = await self.real.read("client-a", self.sid, "message", 0, 100)
            call_id = messages[-1]["payload"]["tool_calls"][0]["id"]
            self.assertIn(call_id, output)
            self.mode = "normal"
            delta = [{"role": "tool", "content": "Synthetic result", "tool_call_id": call_id}]
            if path == "/v1/messages":
                delta = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "Synthetic result"}]}]
            response = await self.infer(path, eid=path + "-result", extra={"messages": delta})
            self.assertEqual(response.status, 200, await response.text())
            messages = await self.real.read("client-a", self.sid, "message", 0, 100)
            self.assertEqual(messages[-2]["payload"]["tool_call_id"], call_id)
            self.assertEqual(messages[-2]["payload"]["tool_name"], "example_tool")

    async def test_real_cancelled_handler_releases_turn_without_answer(self):
        reached = asyncio.Event()
        # Pause the existing relay after raw input was committed; cancellation must
        # release its turn rather than append an invented assistant response.
        from unittest.mock import patch
        from skirnir_router import proxy
        original = proxy._Relay.run

        async def blocked(relay):
            reached.set()
            await asyncio.Future()

        from aiohttp.test_utils import make_mocked_request
        request = make_mocked_request("POST", "/api/chat", app=self.client.server.app,
                                      headers={"Authorization": "Bearer client-a"})
        request["client"] = "client-a"
        body = {"model": "example:latest", "messages": [{"role": "user", "content": "Synthetic question"}],
                "routing": {"session": {"id": str(self.sid), "mode": "skirnir-managed", "event_id": "cancelled"}}}
        with patch.object(proxy._Relay, "run", blocked):
            task = asyncio.create_task(proxy.route_request(request, "/api/chat", copy.deepcopy(body)))
            await asyncio.wait_for(reached.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIs(proxy._Relay.run, original)
        self.assertEqual(len(await self.real.read("client-a", self.sid, "message", 0, 100)), 1)
        async with self.real.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT state FROM skirnir_sessions.turns WHERE owner=$1 AND session_id=$2", "client-a", self.sid), "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
