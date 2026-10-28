#!/usr/bin/env python3
"""Disposable PostgreSQL + synthetic loopback Hindsight and inference integration."""
import asyncio
import os
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from selftest_memory import MemoryHarness, PROJECT
from selftest_context import ManagedHarness
import selftest_interoperability as shared_fixture
from selftest_interoperability import SHARED
from skirnir_router import context_ops, memory, memory_outbox, session_api, state
from skirnir_router.context_config import ContextConfig
from skirnir_router.memory_config import HindsightConfig
from skirnir_router.session_config import SessionConfig
from skirnir_router.outcome import Problem
from skirnir_router.reliability_store import OPERATION_NOT_FOUND
from skirnir_router.session_store import PostgresSessionStore
from skirnir_router.session_types import NOT_FOUND, UNAVAILABLE

DSN = os.environ.get("SKIRNIR_TEST_SESSION_DSN")


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class DurableStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        self.owner = "test-" + uuid4().hex
        self.other = "test-" + uuid4().hex
        self.operation = uuid4()
        self.scope = asdict(memory.Scope(self.owner, PROJECT, "personal", True, True, False))
        self.scope["__bank"] = "skirnir-" + "a" * 64
        self.payload = {"content": "Synthetic knowledge", "confirmed": False, "provenance": {"kind": "assistant_derivation"}}

    async def asyncTearDown(self):
        async with self.store.pool.acquire() as conn:
            await conn.execute("DELETE FROM skirnir_sessions.memory_outbox WHERE owner=ANY($1::text[])", [self.owner, self.other])
            await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=ANY($1::text[])", [self.owner, self.other])
        await self.store.close()

    async def put(self, operation=None, fingerprint="synthetic-fingerprint", capacity=10):
        return await self.store.outbox_put(operation or self.operation, self.scope, self.payload, fingerprint, capacity)

    async def test_verified_delivery_state_machine_and_resubmission_round(self):
        await self.put()
        send = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(send["lease"]), "accepted")
        check = await self.store.outbox_claim(120, 5)
        # A verification keeps 'accepted', counts a check, not a send, and starts the verification clock.
        self.assertEqual((check["state"], check["attempts"], check["verify_checks"]), ("accepted", 1, 1))
        self.assertIsNotNone(check["accepted_at"])
        self.assertIsNone(await self.store.outbox_claim(120, 5))   # leased verification is not claimed twice
        self.assertEqual((await self.store.outbox_finish(self.operation, UUID(send["lease"]), "completed")).status, 409)
        await self.store.outbox_finish(self.operation, UUID(check["lease"]), "pending", 0, error="upstream_failed", upstream_attempts=1)
        row = await self.store.outbox_get(self.owner, self.operation)
        self.assertEqual((row["state"], row["upstream_attempts"], row["retry_base"], row["last_error"]), ("pending", 1, 1, "upstream_failed"))
        self.assertEqual(row["payload"], self.payload)   # kept for the resubmission
        resend = await self.store.outbox_claim(120, 5)
        self.assertEqual((resend["state"], resend["attempts"] - resend["retry_base"]), ("processing", 1))   # new send round
        await self.store.outbox_finish(self.operation, UUID(resend["lease"]), "accepted")
        self.assertEqual((await self.store.outbox_get(self.owner, self.operation))["verify_checks"], 0)
        check = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(check["lease"]), "completed")
        counts = await self.store.outbox_counts()
        self.assertEqual(counts.get("completed"), 1)
        self.assertNotIn("accepted", counts)

    async def test_retry_round_keeps_resubmission_ids_unique(self):
        await self.put()
        send = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(send["lease"]), "accepted")
        check = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(check["lease"]), "dead", error="upstream_failed", upstream_attempts=3)
        await self.store.outbox_retry(self.owner, self.operation, "round-1", self.scope, 10)
        row = await self.store.outbox_get(self.owner, self.operation)
        # The round's resubmission budget starts at the current count; the counter itself never goes back,
        # so no upstream id that Hindsight already marked as failed is ever reused.
        self.assertEqual((row["state"], row["upstream_attempts"], row["upstream_base"]), ("pending", 3, 3))

    async def test_restart_persistence_replay_conflict_and_capacity(self):
        first = await self.put()
        await self.store.close()
        self.store = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        self.assertEqual((await self.store.outbox_get(self.owner, self.operation))["payload"], self.payload)
        self.assertEqual((await self.put())["operation_id"], first["operation_id"])
        self.assertEqual((await self.put(fingerprint="changed")).status, 409)
        self.assertEqual((await self.put(uuid4(), capacity=1)).status, 503)
        self.assertEqual((await self.store.outbox_counts())["pending"], 1)

    async def test_parallel_workers_claim_once_and_stale_lease_cannot_finish(self):
        await self.put()
        claims = await asyncio.gather(*(self.store.outbox_claim(120, 5) for _ in range(5)))
        self.assertEqual(sum(v is not None for v in claims), 1)
        first = next(v for v in claims if v)
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=$1", self.operation)
        second = await self.store.outbox_claim(120, 5)
        self.assertNotEqual(first["lease"], second["lease"])
        self.assertEqual((await self.store.outbox_finish(self.operation, UUID(first["lease"]), "accepted")).status, 409)
        await self.store.outbox_finish(self.operation, UUID(second["lease"]), "accepted")
        self.assertEqual((await self.store.outbox_get(self.owner, self.operation))["attempts"], 2)

    async def test_expired_attempt_limit_becomes_dead_letter(self):
        await self.put()
        await self.store.outbox_claim(120, 1)
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET lease_until=clock_timestamp()-interval '1 second' WHERE operation_id=$1", self.operation)
        self.assertIsNone(await self.store.outbox_claim(120, 1))
        self.assertEqual((await self.store.outbox_get(self.owner, self.operation))["state"], "dead")

    async def test_pending_cancel_is_repeatable_and_ambiguous_cancel_is_blocked(self):
        await self.put()
        self.assertEqual(await self.store.outbox_cancel(self.other, self.operation), OPERATION_NOT_FOUND)
        await self.store.outbox_cancel(self.owner, self.operation)
        await self.store.outbox_cancel(self.owner, self.operation)
        self.assertEqual((await self.put())["state"], "cancelled")
        self.assertIsNone(await self.store.outbox_claim(120, 5))
        operation = uuid4()
        await self.put(operation)
        claim = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(operation, UUID(claim["lease"]), "pending", 10)
        self.assertEqual((await self.store.outbox_cancel(self.owner, operation)).status, 409)

    async def test_backoff_prevents_early_claim(self):
        await self.put()
        claim = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(claim["lease"]), "pending", 3600)
        self.assertIsNone(await self.store.outbox_claim(120, 5))
        value = await self.store.outbox_get(self.owner, self.operation)
        self.assertEqual(value["state"], "pending")

    async def complete(self):
        """Send (accepted), then one verification claim that confirms completion."""
        claim = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(claim["lease"]), "accepted")
        claim = await self.store.outbox_claim(120, 5)
        self.assertEqual((claim["state"], claim["verify_checks"], claim["attempts"]), ("accepted", 1, 1))
        await self.store.outbox_finish(self.operation, UUID(claim["lease"]), "completed")

    async def test_retention_purges_payload_but_keeps_idempotency_tombstone(self):
        await self.put()
        claim = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(claim["lease"]), "accepted", 3600)
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET updated_at=clock_timestamp()-interval '20 days' WHERE operation_id=$1", self.operation)
        # Accepted but unverified: the payload may still be resubmitted, retention must keep it.
        self.assertEqual((await self.store.cleanup(0, 0, 10))["outbox_payloads"], 0)
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET available_at=clock_timestamp() WHERE operation_id=$1", self.operation)
        claim = await self.store.outbox_claim(120, 5)
        await self.store.outbox_finish(self.operation, UUID(claim["lease"]), "completed")
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET updated_at=clock_timestamp()-interval '20 days' WHERE operation_id=$1", self.operation)
        value = await self.store.cleanup(0, 0, 10)
        self.assertEqual(value["outbox_payloads"], 1)
        row = await self.put()
        self.assertEqual(row["state"], "completed")
        self.assertIsNone(row["payload"])
        self.assertIsNone(await self.store.outbox_claim(120, 5))

    async def test_discovery_owner_scope_metadata_minimization_and_update_cursor(self):
        session = await self.store.create(self.owner, PROJECT, "personal", {"private": "Synthetic metadata"})
        sid = UUID(session["id"])
        await self.store.create(self.other, PROJECT, "personal", {})
        await self.store.append(self.owner, sid, "message-1", "message", {"role": "user", "content": "Synthetic"})
        rows = await self.store.discover(self.owner, [PROJECT], PROJECT, "active", None, datetime.max.replace(tzinfo=timezone.utc), UUID(int=(1 << 128) - 1), 1, "example")
        self.assertEqual([r["id"] for r in rows], [str(sid)])
        self.assertNotIn("metadata", rows[0])
        self.assertGreaterEqual(rows[0]["updated_at"], session["updated_at"])
        rows = await self.store.discover(self.owner, [], None, None, None, datetime.max.replace(tzinfo=timezone.utc), UUID(int=(1 << 128) - 1), 10, "")
        self.assertEqual(rows, [])

    async def test_import_atomic_idempotent_and_events_preserved(self):
        messages = [{"role": "user", "content": "Untrusted synthetic handoff"}]
        checkpoint = {"schema_version": 1, "open_steps": ["Synthetic step"]}
        args = (self.owner, "import-1", "fingerprint", PROJECT, "personal", "skirnir-managed", {}, messages, checkpoint)
        first, created = await self.store.import_session(*args)
        replay, again = await self.store.import_session(*args)
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first["id"], replay["id"])
        sid = UUID(first["id"])
        self.assertEqual((await self.store.read(self.owner, sid, "message", 0, 10))[0]["payload"], messages[0])
        self.assertEqual((await self.store.read(self.owner, sid, "checkpoint", 0, 10))[0]["payload"], checkpoint)
        self.assertEqual((await self.store.import_session(self.owner, "import-1", "other", *args[3:])).status, 409)
        # Driver encoding failure occurs after inserting the session and messages.
        self.assertEqual(await self.store.import_session(self.owner, "invalid", "x", PROJECT, "personal", "skirnir-managed", {}, messages,
                                                         {"schema_version": 1, "invalid": object()}), UNAVAILABLE)
        async with self.store.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM skirnir_sessions.handoff_imports WHERE owner=$1", self.owner), 1)

    async def test_auto_checkpoint_is_atomic_deduplicated_and_versioned(self):
        session = await self.store.create(self.owner, PROJECT, "personal", {}, mode="skirnir-managed")
        sid = UUID(session["id"])
        cfg = ContextConfig(enabled=True)
        checkpoint = {"schema_version": 1, "open_steps": ["External work unverified"], "provenance": {"source": "skirnir-observed"}}
        for index in range(2):
            claim = await self.store.claim(self.owner, sid, str(index), str(index), [{"role": "user", "content": "Synthetic question"}], cfg)
            await self.store.finish_turn(self.owner, sid, claim["lease"], {"role": "assistant", "content": "Synthetic answer"}, checkpoint=checkpoint)
        self.assertEqual(len(await self.store.read(self.owner, sid, "message", 0, 100)), 4)
        self.assertEqual(len(await self.store.read(self.owner, sid, "checkpoint", 0, 100)), 1)
        claim = await self.store.claim(self.owner, sid, "third", "third", [{"role": "user", "content": "Synthetic next question"}], cfg)
        await self.store.finish_turn(self.owner, sid, claim["lease"], {"role": "assistant", "content": "Synthetic next answer"}, checkpoint={**checkpoint, "open_steps": ["Other synthetic step"]})
        self.assertEqual([r["version"] for r in await self.store.read(self.owner, sid, "checkpoint", 0, 100)], [1, 2])

    async def test_session_retention_skips_active_leases_and_preserves_independent_memory(self):
        await self.put()
        archived = await self.store.create(self.owner, PROJECT, "personal", {})
        active = await self.store.create(self.owner, PROJECT, "personal", {}, mode="skirnir-managed")
        archived_id, active_id = UUID(archived["id"]), UUID(active["id"])
        await self.store.update(self.owner, archived_id, "archived", None)
        turn = await self.store.claim(self.owner, active_id, "one", "one", [{"role": "user", "content": "Synthetic"}], ContextConfig(enabled=True))
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.sessions SET updated_at=clock_timestamp()-interval '20 days' WHERE owner=$1", self.owner)
        result = await self.store.cleanup(10, 5, 0)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual((await self.store.get(self.owner, active_id))["status"], "active")
        self.assertEqual((await self.store.outbox_get(self.owner, self.operation))["state"], "pending")
        await self.store.fail_turn(self.owner, active_id, turn["lease"])
        self.assertEqual((await self.store.cleanup(10, 5, 0))["sessions"], 1)

    async def test_per_owner_capacity_is_atomic_and_does_not_block_other_clients(self):
        async def submit():
            return await self.store.outbox_put(uuid4(), self.scope, self.payload, "synthetic", 10, owner_capacity=2)
        values = await asyncio.gather(*(submit() for _ in range(6)))
        self.assertEqual(sum(isinstance(v, dict) for v in values), 2)
        self.assertEqual(sum(isinstance(v, Problem) and v.status == 503 for v in values), 4)
        scope = {**self.scope, 'principal': self.other}
        await self.store.outbox_put(uuid4(), scope, self.payload, 'synthetic-other', 10, owner_capacity=2)
        self.assertEqual((await self.store.outbox_counts())['pending'], 3)

    async def test_retry_preserves_uuid_and_total_attempts_and_old_retry_keys(self):
        await self.put()
        claim = await self.store.outbox_claim(120, 1)
        await self.store.outbox_finish(self.operation, UUID(claim['lease']), 'dead', error='timeout')
        first = await self.store.outbox_retry(self.owner, self.operation, 'recovery-1', self.scope, 10)
        self.assertEqual(first['attempts'], 1)
        self.assertEqual(first['retry_base'], 1)
        self.assertEqual(first['retry_rounds'], 1)
        replay = await self.store.outbox_retry(self.owner, self.operation, 'recovery-1', self.scope, 10)
        self.assertEqual(replay['retry_rounds'], 1)
        self.assertEqual((await self.store.outbox_cancel(self.owner, self.operation)).status, 409)
        claim = await self.store.outbox_claim(120, 1)
        self.assertEqual(claim['attempts'], 2)
        await self.store.outbox_finish(self.operation, UUID(claim['lease']), 'dead', error='timeout')
        await self.store.outbox_retry(self.owner, self.operation, 'recovery-2', self.scope, 10)
        replay = await self.store.outbox_retry(self.owner, self.operation, 'recovery-1', self.scope, 10)
        self.assertEqual(replay['retry_rounds'], 2)
        self.assertEqual(replay['operation_id'], str(self.operation))

    async def test_retry_rejects_foreign_cancelled_purged_and_changed_bank(self):
        await self.put()
        claim = await self.store.outbox_claim(120, 1)
        await self.store.outbox_finish(self.operation, UUID(claim['lease']), 'dead')
        self.assertEqual(await self.store.outbox_retry(self.other, self.operation, 'one', self.scope, 10), OPERATION_NOT_FOUND)
        self.assertEqual((await self.store.outbox_retry(self.owner, self.operation, 'one', {**self.scope, '__bank': 'changed'}, 10)).status, 409)
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET payload=NULL WHERE operation_id=$1", self.operation)
        self.assertEqual((await self.store.outbox_retry(self.owner, self.operation, 'one', self.scope, 10)).status, 409)

    async def test_totals_remain_exact_on_transitions_and_transaction_rollback(self):
        await self.put()
        self.assertGreaterEqual(await self.store.outbox_oldest(), 0)
        claim = await self.store.outbox_claim(120, 5)
        self.assertEqual((await self.store.outbox_counts())['processing'], 1)
        async with self.store.pool.acquire() as conn:
            transaction = conn.transaction()
            await transaction.start()
            await conn.execute("DELETE FROM skirnir_sessions.memory_outbox WHERE operation_id=$1", self.operation)
            await transaction.rollback()
        self.assertEqual((await self.store.outbox_counts())['processing'], 1)
        await self.store.outbox_finish(self.operation, UUID(claim['lease']), 'accepted')
        self.assertEqual((await self.store.outbox_counts())['accepted'], 1)
        self.assertNotIn('processing', await self.store.outbox_counts())
        self.assertEqual(await self.store.outbox_oldest(), 0)

    async def test_compacted_tombstone_keeps_binding_and_deletion_is_not_unsent(self):
        await self.put()
        await self.complete()
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET updated_at=clock_timestamp()-interval '20 days' WHERE operation_id=$1", self.operation)
        await self.store.cleanup(0, 0, 10)
        row = await self.store.outbox_get(self.owner, self.operation)
        self.assertEqual(row['scope'], {'__bank': self.scope['__bank']})
        await self.store.outbox_delete(self.owner, self.operation, self.scope)
        row = await self.store.outbox_get(self.owner, self.operation)
        self.assertEqual(row['state'], 'delete_pending')
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['retry_base'], 1)


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class DurableHttpTests(MemoryHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        shared_fixture.SharedGatewayTests.shared(self)
        self.service.worker.cancel()
        await asyncio.gather(self.service.worker, return_exceptions=True)
        self.service.worker = None
        self.real = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        self.service.outbox = self.real
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_enabled=True)

    async def asyncTearDown(self):
        async with self.real.pool.acquire() as conn:
            await conn.execute("DELETE FROM skirnir_sessions.memory_outbox WHERE owner=ANY($1::text[])", ["client-a", "client-b"])
        await super().asyncTearDown()
        await self.real.close()

    async def request(self, path, payload=None, token="client-a", method="post"):
        return await getattr(self.client, method)(path, json=payload, headers={"Authorization": "Bearer " + token})

    async def retain(self, event="one"):
        return await self.request(memory.PREFIX + "/retain", {"project": SHARED, "event_id": event, "content": "Synthetic shared knowledge", "confirmed": True})

    async def test_real_restart_and_worker_replay_with_shared_readers(self):
        first = await self.retain()
        value = await first.json()
        self.assertEqual(first.status, 202)
        self.assertTrue(value["durable"])
        operation = UUID(value["operation_id"])
        await self.real.close()
        self.real = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        self.service.outbox = self.real
        self.assertEqual((await self.real.outbox_get("client-a", operation))["state"], "pending")
        await memory_outbox.step(self.service)
        replay = await self.retain()
        self.assertEqual((await replay.json())["state"], "accepted")
        self.assertFalse(await memory_outbox.step(self.service))
        self.assertEqual(len(self.operations), 1)
        for token in ("client-a", "client-b"):
            response = await self.request(memory.PREFIX + "/recall", {"project": SHARED, "query": "Synthetic"}, token)
            self.assertEqual(response.status, 200)
        self.assertEqual(self.calls[-2][0], self.calls[-1][0])
        self.assertEqual((await self.request(memory.PREFIX + "/recall", {"project": SHARED, "query": "Synthetic"}, "client-owned")).status, 403)

    async def test_real_bounded_retry_and_runtime_revoke(self):
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_attempts=2)
        operation = UUID((await (await self.retain()).json())["operation_id"])
        self.retain_failures = 20
        await memory_outbox.step(self.service)
        self.assertEqual((await self.real.outbox_get("client-a", operation))["state"], "pending")
        self.assertFalse(await memory_outbox.step(self.service))
        async with self.real.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET available_at=clock_timestamp() WHERE operation_id=$1", operation)
        await memory_outbox.step(self.service)
        self.assertEqual((await self.real.outbox_get("client-a", operation))["state"], "dead")
        self.assertEqual(len(self.operations), 1)
        second = UUID((await (await self.retain("second")).json())["operation_id"])
        state.CFG.client_auth["clients"]["client-a"]["memory"]["projects"][SHARED]["write"] = False
        before = len(self.calls)
        await memory_outbox.step(self.service)
        self.assertEqual((await self.real.outbox_get("client-a", second))["state"], "revoked")
        self.assertEqual(len(self.calls), before)

    async def test_memory_deletion_waits_for_remote_completion_and_preserves_other_sources(self):
        operation = (await (await self.retain()).json())["operation_id"]
        await memory_outbox.step(self.service)
        path = memory.PREFIX + "/operations/" + operation + "/delete"
        reader = state.CFG.client_auth["clients"]["client-b"]["memory"]["projects"][SHARED]
        reader["delete"] = False   # a reader of the shared scope, not its curator: foreign operations stay invisible
        self.assertEqual((await self.request(path, token="client-b")).status, 404)
        # The real store finds the operation for a curator check, whoever owns it.
        self.assertEqual((await self.real.outbox_find(UUID(operation)))["owner"], "client-a")
        self.assertEqual((await self.real.outbox_find(uuid4())).status, 404)
        spec = state.CFG.client_auth["clients"]["client-a"]["memory"]["projects"][SHARED]
        spec["delete"] = False
        self.assertEqual((await self.request(path)).status, 403)
        spec["delete"] = True
        self.operation_state = "processing"
        self.assertEqual((await self.request(path)).status, 202)
        await memory_outbox.step(self.service)
        self.assertEqual(self.deleted_documents, [])
        self.assertEqual((await self.real.outbox_get("client-a", UUID(operation)))["state"], "delete_pending")
        self.operation_state = "completed"
        async with self.real.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET available_at=clock_timestamp() WHERE operation_id=$1", UUID(operation))
        await memory_outbox.step(self.service)
        row = await self.real.outbox_get("client-a", UUID(operation))
        self.assertEqual(row["state"], "deleted")
        self.assertIsNone(row["payload"])
        self.assertEqual(self.deleted_documents[-1][1], operation)
        self.assertEqual((await (await self.retain()).json())["state"], "deleted")
        self.assertFalse(await memory_outbox.step(self.service))

    async def test_auto_retention_uses_durable_outbox_and_is_unverified(self):
        spec = state.CFG.client_auth["clients"]["client-a"]["memory"]
        spec.update(strategy="auto", default_project=SHARED, auto_retain=True)
        response = await self.infer(enabled=False, extra={"routing": {"request_id": "auto-retain"}})
        self.assertEqual(response.status, 200, await response.text())
        counts = await self.real.outbox_counts()
        self.assertEqual(counts["pending"], 1)
        await memory_outbox.step(self.service)
        item = next(iter(self.operations.values()))["items"][0]
        self.assertEqual(item["metadata"]["confirmed"], "false")
        self.assertEqual(item["metadata"]["kind"], "assistant_derivation")


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class ExchangeHttpTests(ManagedHarness):
    async def asyncSetUp(self):
        self.real = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        await super().asyncSetUp()
        state.CFG.session_store = replace(state.CFG.session_store, handoff_enabled=True)
        state.CFG.context_manager = replace(state.CFG.context_manager, auto_checkpoints=True)
        state.CFG.client_auth["clients"]["client-b"]["session_handoff_from"] = ["client-a"]
        self.key_env = patch.dict(os.environ, {"SKIRNIR_HANDOFF_KEY": "synthetic-handoff-key-32-bytes-minimum"})
        self.key_env.start()
        self.session = await self.real.create("client-a", PROJECT, "personal", {"handoff": {"branch": "example"}}, mode="skirnir-managed")
        self.sid = UUID(self.session["id"])
        self.client.server.app[session_api.STORE_KEY] = self.real

    async def asyncTearDown(self):
        async with self.real.pool.acquire() as conn:
            await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=ANY($1::text[])", ["client-a", "client-b"])
        await super().asyncTearDown()
        self.key_env.stop()
        await self.real.close()

    async def test_export_import_discovery_continuation_and_auto_checkpoints(self):
        response = await self.infer(eid="initial")
        self.assertEqual(response.status, 200, await response.text())
        checkpoints = await self.real.read("client-a", self.sid, "checkpoint", 0, 10)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["payload"]["provenance"]["source"], "skirnir-observed")
        response = await self.client.get(session_api.PREFIX + "/" + str(self.sid) + "/handoff", headers={"Authorization": "Bearer client-a"})
        self.assertEqual(response.status, 200, await response.text())
        package = await response.json()
        body = {"event_id": "import-1", "package": package, "project": PROJECT, "mode": "skirnir-managed"}
        response = await self.client.post(session_api.PREFIX + "/import", json=body, headers={"Authorization": "Bearer client-b"})
        self.assertEqual(response.status, 201, await response.text())
        imported = await response.json()
        self.assertEqual(imported["owner"], "client-b")
        self.assertFalse(imported["metadata"]["resumable_execution"])
        response = await self.client.post(session_api.PREFIX + "/import", json=body, headers={"Authorization": "Bearer client-b"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["id"], imported["id"])
        response = await self.client.get(session_api.PREFIX + "?project=" + PROJECT, headers={"Authorization": "Bearer client-b"})
        self.assertEqual([v["id"] for v in (await response.json())["items"]], [imported["id"]])
        raw = await self.real.read("client-b", UUID(imported["id"]), "message", 0, 100)
        self.assertTrue(all(v["payload"]["role"] == "user" for v in raw))
        self.assertTrue(all("Untrusted handoff" in v["payload"]["content"] for v in raw))
        self.sid = UUID(imported["id"])
        self.assertEqual((await self.infer(token="client-b", eid="continued")).status, 200)

    async def test_checkpoints_remain_stable_and_tool_calls_are_unverified(self):
        for event in ("first", "second"):
            response = await self.infer(eid=event)
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len(await self.real.read("client-a", self.sid, "checkpoint", 0, 100)), 1)
        self.mode = "tool"
        response = await self.infer(stream=True, eid="tool")
        self.assertNotIn('"error"', await response.text())
        checkpoints = await self.real.read("client-a", self.sid, "checkpoint", 0, 100)
        self.assertEqual(len(checkpoints), 2)
        self.assertIn("unverified", checkpoints[-1]["payload"]["last_result"])
        self.assertTrue(checkpoints[-1]["payload"]["open_steps"])


RESET = "Resetting connection with an active transaction"


class HoldingRelay:
    """Loopback TCP relay to the test database. Armed, it holds the next client chunk carrying a BEGIN (and what follows
    on that connection) until released, so a test can stop a task exactly while its transaction is starting."""
    def __init__(self, host, port):
        self.target = (host, port)
        self.armed = False
        self.held, self.released = asyncio.Event(), asyncio.Event()

    async def start(self):
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def accept(self, reader, writer):
        upstream_reader, upstream_writer = await asyncio.open_connection(*self.target)
        await asyncio.gather(self.pipe(reader, upstream_writer, True), self.pipe(upstream_reader, writer, False), return_exceptions=True)

    async def pipe(self, reader, writer, outbound):
        while data := await reader.read(65536):
            if outbound and self.armed and b"BEGIN" in data:
                self.armed = False
                self.held.set()
                await self.released.wait()
            writer.write(data)
            await writer.drain()
        writer.close()

    async def close(self):
        self.released.set()
        self.server.close()


class StubHindsight:
    async def close(self):
        pass


def outbox_count(outcome):
    series = state.METRICS["counters"].get("skirnir_memory_events_total", {})
    return series.get(tuple(sorted({"operation": "outbox", "outcome": outcome}.items())), 0)


class RelayedStoreCase(unittest.IsolatedAsyncioTestCase):
    """A store whose connections run through a HoldingRelay; the loop's exception reports are collected."""
    async def asyncSetUp(self):
        self.previous = state.CFG
        target = urlsplit(DSN)
        self.relay = HoldingRelay(target.hostname or "127.0.0.1", target.port or 5432)
        port = await self.relay.start()
        credentials = target.netloc.rpartition("@")[0]
        relayed = target._replace(netloc=(credentials + "@" if credentials else "") + f"127.0.0.1:{port}").geturl()
        self.store = await PostgresSessionStore.open(relayed, SessionConfig(enabled=True))
        self.assertIsInstance(self.store, PostgresSessionStore)
        self.reports = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: self.reports.append(context["message"]))

    async def asyncTearDown(self):
        self.relay.released.set()
        await self.store.close()
        await self.relay.close()
        asyncio.get_running_loop().set_exception_handler(None)
        state.CFG = self.previous

    def resets(self):
        """The pool's reports about a connection returned in a transaction (asyncpg appends the connection's repr)."""
        return [message for message in self.reports if message.startswith(RESET)]


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class InterruptedBeginTests(RelayedStoreCase):
    """A store call interrupted while its BEGIN is on the way must not return the connection to the pool with asyncpg's
    transaction marker on it. Stopping the router no longer does that (OrderedShutdownTests); two causes remain: aiohttp
    3.8 cancels a handler whose client disconnected, and the store's own timeout can fire during BEGIN."""
    async def interrupted_read(self, interrupt, hold_s=1.0):
        """Start a read whose BEGIN the relay holds, `interrupt` it, and release the BEGIN after hold_s. Returns the call
        and whether it was over before the release. hold_s stays well below the pool's command_timeout (5 s): asyncpg
        waits that long for the answer to a cancelled query and then drops the connection itself, hiding the fault."""
        self.relay.armed = True
        call = asyncio.ensure_future(self.store.read("test-" + uuid4().hex, uuid4(), "message", 0, 10))
        await asyncio.wait_for(self.relay.held.wait(), 5)
        interrupt(call)
        over, _ = await asyncio.wait({call}, timeout=hold_s)
        self.relay.released.set()
        await asyncio.wait({call}, timeout=10)
        return call, bool(over)

    async def assert_store_usable(self):
        """The store answers again, and the interrupted connection went back to the pool once: each of the pool's
        connections is handed out once, and no further one."""
        self.assertEqual(await self.store.read("test-" + uuid4().hex, uuid4(), "message", 0, 10), NOT_FOUND)
        pool = self.store.pool
        held = [await pool.acquire(timeout=5) for _ in range(pool.get_max_size())]
        extra = asyncio.ensure_future(pool.acquire())
        over, _ = await asyncio.wait({extra}, timeout=0.3)
        pids = {conn.get_server_pid() for conn in held}
        for conn in held:
            await pool.release(conn)
        await pool.release(await extra)
        self.assertEqual(len(pids), len(held))
        self.assertEqual(over, set())

    async def test_cancelled_call_during_begin_does_not_return_an_open_transaction(self):
        call, over_while_held = await self.interrupted_read(lambda call: call.cancel())   # the handler's client left
        self.assertEqual(self.resets(), [])
        self.assertEqual(self.reports, [])
        self.assertTrue(over_while_held)   # the cancelled caller does not wait for the held BEGIN
        self.assertTrue(call.cancelled())
        await self.assert_store_usable()

    async def test_timeout_during_begin_does_not_return_an_open_transaction(self):
        self.store.timeout_s = 0.3   # fires while the relay holds the BEGIN
        call, over_while_held = await self.interrupted_read(lambda call: None)
        self.store.timeout_s = SessionConfig().timeout_s
        self.assertEqual(self.resets(), [])
        self.assertEqual(self.reports, [])
        self.assertTrue(over_while_held)   # the timeout bounds the call, not the server's answer to the BEGIN
        self.assertEqual(call.result(), UNAVAILABLE)
        await self.assert_store_usable()


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class OrderedShutdownTests(RelayedStoreCase):
    """Stopping the router must not cut a transaction off. A task cancelled while its BEGIN was on the way left asyncpg's
    transaction marker on the connection, and returning it to the pool logged 'Resetting connection with an active
    transaction' (router 0.8.1, deploy 2026-10-19). The background tasks stop first and the pool closes after them."""
    def outbox_service(self, **settings):
        cfg = HindsightConfig(enabled=True, allow_http_loopback=True, retention_enabled=True, outbox_enabled=True, **settings)
        state.CFG = SimpleNamespace(hindsight=cfg)
        service = memory.MemoryService(cfg, b"synthetic-namespace-key-32-bytes", StubHindsight())
        service.outbox = self.store
        self.relay.armed = True
        service.worker = asyncio.create_task(service.run_worker())   # the first claim begins a transaction at once
        return service

    async def stop_while_beginning(self, stop, hold_s=0.2):
        """Start `stop` while the task's BEGIN is held by the relay, release the BEGIN after hold_s, wait for `stop`."""
        await asyncio.wait_for(self.relay.held.wait(), 5)
        stopping = asyncio.ensure_future(stop)
        await asyncio.sleep(hold_s)
        self.relay.released.set()
        await asyncio.wait_for(stopping, 10)

    async def test_outbox_worker_ends_its_transaction_before_the_pool_closes(self):
        service = self.outbox_service()
        before = outbox_count("shutdown_cancel")
        await self.stop_while_beginning(service.close())
        self.assertEqual(self.resets(), [])
        self.assertTrue(service.worker.done())
        self.assertFalse(service.worker.cancelled())   # it ended on its own after the claim
        self.assertEqual(outbox_count("shutdown_cancel"), before)

    async def test_outbox_worker_outlasting_the_timeout_is_cancelled_and_counted(self):
        service = self.outbox_service(timeout_s=0.2)
        before = outbox_count("shutdown_cancel")
        await self.stop_while_beginning(service.close(), hold_s=0.6)   # held longer than timeout_s
        self.assertTrue(service.worker.done())
        self.assertEqual(outbox_count("shutdown_cancel"), before + 1)

    async def test_maintenance_ends_its_cleanup_before_the_pool_closes(self):
        state.CFG = SimpleNamespace(session_store=SessionConfig(enabled=True, retention_days=36500), context_manager=ContextConfig(),
                                    hindsight=HindsightConfig())
        lifecycle = context_ops.lifecycle({session_api.STORE_KEY: self.store})
        await anext(lifecycle)
        self.relay.armed = True   # poll() runs no transaction: the next BEGIN is the cleanup's
        await self.stop_while_beginning(anext(lifecycle, None))
        self.assertEqual(self.resets(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
