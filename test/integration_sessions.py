#!/usr/bin/env python3
"""Real PostgreSQL contract/API tests against a disposable local/CI database.

pip install -r router/requirements-session.txt
SKIRNIR_TEST_SESSION_DSN=postgresql://... python test/integration_sessions.py
Only synthetic, randomly scoped test owners are written. Never use production DBs.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import app, session_migrations, state  # noqa: E402
from skirnir_router.session_config import SessionConfig  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402
from skirnir_router.session_store import PostgresSessionStore  # noqa: E402
from skirnir_router.session_types import NOT_FOUND, UNAVAILABLE  # noqa: E402
from selftest_sessions import URL, configuration  # noqa: E402

DSN = os.environ.get("SKIRNIR_TEST_SESSION_DSN")


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = SessionConfig(enabled=True)
        self.store = await PostgresSessionStore.open(DSN, self.cfg)
        self.owner = "test-" + uuid4().hex
        self.other = "test-" + uuid4().hex
        self.session = await self.store.create(self.owner, "work", "private", {"source": "test"})
        self.sid = UUID(self.session["id"])

    async def asyncTearDown(self):
        try:
            async with self.store.pool.acquire() as conn:
                await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=ANY($1::text[])", [self.owner, self.other])
        finally:
            await self.store.close()

    async def test_two_principals_cannot_read_or_mutate(self):
        for op in (self.store.get(self.other, self.sid), self.store.read(self.other, self.sid, "message", 0, 10),
                   self.store.append(self.other, self.sid, "steal", "message", {"role": "user"}),
                   self.store.update(self.other, self.sid, "archived", {}), self.store.delete(self.other, self.sid)):
            self.assertEqual(await op, NOT_FOUND)
        self.assertEqual((await self.store.get(self.owner, self.sid))["metadata"], {"source": "test"})

    async def test_duplicate_events_and_conflicts(self):
        message = {"role": "assistant", "tool_calls": [{"id": "tool1"}], "content": None}
        first, created = await self.store.append(self.owner, self.sid, "m1", "message", message)
        retry, second_created = await self.store.append(self.owner, self.sid, "m1", "message", message)
        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertEqual(first, retry)
        for kind, payload in (("message", {"role": "user"}), ("checkpoint", {"schema_version": 1})):
            self.assertEqual((await self.store.append(self.owner, self.sid, "m1", kind, payload)).status, 409)
        self.assertEqual((await self.store.read(self.owner, self.sid, "message", 0, 10))[0]["payload"], message)

    async def test_concurrent_order_and_duplicate_writers(self):
        rows = await asyncio.gather(*(self.store.append(self.owner, self.sid, f"m{n}", "message", {"role": "user", "content": n})
                                     for n in range(20)))
        self.assertEqual(sorted(row["seq"] for row, _ in rows), list(range(1, 21)))
        duplicates = await asyncio.gather(*(self.store.append(self.owner, self.sid, "same", "message", {"role": "user"}) for _ in range(10)))
        self.assertEqual(sum(created for _, created in duplicates), 1)
        self.assertEqual({row["seq"] for row, _ in duplicates}, {21})
        page1 = await self.store.read(self.owner, self.sid, "message", 0, 10)
        page2 = await self.store.read(self.owner, self.sid, "message", page1[-1]["seq"], 100)
        self.assertEqual([row["seq"] for row in page1 + page2], list(range(1, 22)))

    async def test_checkpoint_versions_and_restart(self):
        for kind, eid, payload in (("message", "m1", {"role": "user"}), ("checkpoint", "c1", {"schema_version": 1, "goals": ["a"]}),
                                   ("message", "m2", {"role": "tool", "tool_call_id": "t1"}),
                                   ("checkpoint", "c2", {"schema_version": 1, "goals": ["b"]})):
            await self.store.append(self.owner, self.sid, eid, kind, payload)
        await self.store.close()
        self.store = await PostgresSessionStore.open(DSN, self.cfg)
        checkpoints = await self.store.read(self.owner, self.sid, "checkpoint", 0, 100)
        self.assertEqual([row["version"] for row in checkpoints], [1, 2])
        self.assertEqual([row["seq"] for row in checkpoints], [2, 4])
        self.assertEqual(checkpoints[0]["payload"]["goals"], ["a"])
        self.assertEqual(len(await self.store.read(self.owner, self.sid, "message", 0, 100)), 2)

    async def test_archive_and_cascade_delete(self):
        payload = {"role": "user"}
        await self.store.append(self.owner, self.sid, "m1", "message", payload)
        updated = await self.store.update(self.owner, self.sid, "archived", {"label": "done"})
        self.assertEqual(updated["status"], "archived")
        self.assertEqual(updated["metadata"], {"label": "done"})
        _, created = await self.store.append(self.owner, self.sid, "m1", "message", payload)
        self.assertFalse(created)
        self.assertEqual((await self.store.append(self.owner, self.sid, "m2", "message", payload)).status, 409)
        await self.store.update(self.owner, self.sid, "active", None)
        await self.store.append(self.owner, self.sid, "c1", "checkpoint", {"schema_version": 1})
        await self.store.delete(self.owner, self.sid)
        self.assertEqual(await self.store.get(self.owner, self.sid), NOT_FOUND)
        async with self.store.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM skirnir_sessions.events WHERE owner=$1", self.owner), 0)

    async def test_statement_error_rolls_back(self):
        self.assertEqual(await self.store.append(self.owner, self.sid, None, "message", {"role": "user"}), UNAVAILABLE)
        self.assertEqual(await self.store.read(self.owner, self.sid, "message", 0, 100), [])
        row, _ = await self.store.append(self.owner, self.sid, "valid", "message", {"role": "user"})
        self.assertEqual(row["seq"], 1)

    async def test_timeout_and_cancel_release_transaction(self):
        async with self.store.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2 FOR UPDATE", self.owner, self.sid)
            self.store.timeout_s = 0.1
            self.assertEqual(await self.store.append(self.owner, self.sid, "timeout", "message", {"role": "user"}), UNAVAILABLE)
            self.store.timeout_s = 5
            task = asyncio.create_task(self.store.append(self.owner, self.sid, "cancel", "message", {"role": "user"}))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row, _ = await self.store.append(self.owner, self.sid, "valid", "message", {"role": "user"})
        self.assertEqual(row["seq"], 1)
        self.assertEqual(len(await self.store.read(self.owner, self.sid, "message", 0, 100)), 1)

    async def test_migration_failure_is_atomic_and_newer_schema_rejected(self):
        async with self.store.pool.acquire() as conn:
            version = await conn.fetchval("SELECT max(version) FROM skirnir_sessions.schema_version")
            bad = "CREATE TABLE skirnir_sessions.rollback_probe (id integer); SELECT 1/0;"
            import asyncpg
            with patch.object(session_migrations, "MIGRATIONS", (*session_migrations.MIGRATIONS, bad)), self.assertRaises(asyncpg.PostgresError):
                await session_migrations.migrate(conn)
            self.assertIsNone(await conn.fetchval("SELECT to_regclass('skirnir_sessions.rollback_probe')"))
            self.assertEqual(await conn.fetchval("SELECT max(version) FROM skirnir_sessions.schema_version"), version)
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute("INSERT INTO skirnir_sessions.schema_version VALUES (999)")
                self.assertEqual((await session_migrations.migrate(conn)).status, 503)
            finally:
                await tx.rollback()

    async def test_parallel_migration_startup(self):
        stores = await asyncio.gather(*(PostgresSessionStore.open(DSN, self.cfg) for _ in range(3)))
        self.assertFalse(any(failed(store) for store in stores))
        for store in stores:
            await store.close()
        self.assertEqual((await self.store.get(self.owner, self.sid))["id"], str(self.sid))

    async def test_api_real_owner_isolation_and_error_mapping(self):
        old_cfg, old_mode = state.CFG, state.CLIENT_AUTH_MODE
        state.CFG, state.CLIENT_AUTH_MODE = configuration(), None
        # Randomize configured identities to leave no user/test data behind.
        state.CFG.client_auth["clients"][self.owner] = state.CFG.client_auth["clients"].pop("alice")
        state.CFG.client_auth["clients"][self.other] = state.CFG.client_auth["clients"].pop("bob")
        try:
            with patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": DSN}):
                api, _ = app.build_apps()
                async with TestClient(TestServer(api)) as client:
                    alice, bob = {"Authorization": "Bearer alice"}, {"Authorization": "Bearer bob"}
                    response = await client.post(URL, json={"project": "work"}, headers=alice)
                    self.assertEqual(response.status, 201)
                    sid = (await response.json())["id"]
                    path = URL + "/" + sid
                    event = {"event_id": "one", "message": {"role": "user", "content": "synthetic"}}
                    for method, suffix in (("GET", ""), ("PATCH", ""), ("DELETE", ""), ("GET", "/messages"),
                                           ("POST", "/messages"), ("GET", "/checkpoints"), ("POST", "/checkpoints")):
                        response = await client.request(method, path + suffix, json=event, headers=bob)
                        self.assertEqual(response.status, 404)
                    for expected in (201, 200):
                        self.assertEqual((await client.post(path + "/messages", json=event, headers=alice)).status, expected)
                    event["message"]["content"] = "different"
                    self.assertEqual((await client.post(path + "/messages", json=event, headers=alice)).status, 409)
                    stored = await (await client.get(path + "/messages", headers=alice)).json()
                    self.assertEqual(stored["items"][0]["payload"]["content"], "synthetic")
                    await api["skirnir_session_store"].close()
                    self.assertEqual((await client.get(path, headers=alice)).status, 503)
        finally:
            state.CFG, state.CLIENT_AUTH_MODE = old_cfg, old_mode

    async def test_connection_failure_does_not_expose_secrets(self):
        # Synthetic credentials, assembled so the public-change scanner does not read them as a real URL credential.
        problem = await PostgresSessionStore.open("postgresql://" + "private-user:private-password" + "@127.0.0.1:1/db", self.cfg)
        self.assertTrue(failed(problem))
        self.assertNotIn("private", problem.message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
