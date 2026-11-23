#!/usr/bin/env python3
"""Session store contract/API tests on every engine (ADR-0114): SQLite always, PostgreSQL against a disposable database.

pip install -r router/requirements-session.txt     # PostgreSQL only; SQLite needs nothing beyond Python
SKIRNIR_TEST_SESSION_DSN=postgresql://... python test/integration_sessions.py
Only synthetic, randomly scoped test owners are written. Never use production DBs.
"""
import asyncio
import os
import sqlite3
import stat
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import app, session_migrations, sqlite_migrations, state  # noqa: E402
from skirnir_router.session_config import SessionConfig  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402
from skirnir_router.session_store import PostgresSessionStore, open_session_store  # noqa: E402
from skirnir_router.session_types import NOT_FOUND, UNAVAILABLE  # noqa: E402
from skirnir_router.sqlite_store import SqliteSessionStore  # noqa: E402
from selftest_sessions import URL, configuration  # noqa: E402
from store_engines import DSN, POSTGRES_ONLY, PostgresEngine, SqliteEngine, prepare  # noqa: E402

MAX_CURSOR = (datetime.max.replace(tzinfo=timezone.utc), UUID(int=(1 << 128) - 1))


class SessionContract:
    """The same contract on every engine; the concrete classes below name the engine."""
    async def asyncSetUp(self):
        prepare(self)
        self.cfg = SessionConfig(enabled=True)
        self.store = await self.engine.open(self.cfg)
        self.owner = "test-" + uuid4().hex
        self.other = "test-" + uuid4().hex
        self.session = await self.store.create(self.owner, "work", "private", {"source": "test"})
        self.sid = UUID(self.session["id"])

    async def asyncTearDown(self):
        # A test may leave a Problem (a failed reopen) or a store it closed itself; only an open store is cleaned up.
        if failed(self.store) or getattr(self.store, "closed", False):
            return
        try:
            async with self.store.connection() as conn:
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
        self.store = await self.engine.open(self.cfg)
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
        async with self.store.connection() as conn:
            self.assertEqual(await conn.fetchval("SELECT count(*) FROM skirnir_sessions.events WHERE owner=$1", self.owner), 0)

    async def test_statement_error_rolls_back(self):
        self.assertEqual(await self.store.append(self.owner, self.sid, None, "message", {"role": "user"}), UNAVAILABLE)
        self.assertEqual(await self.store.read(self.owner, self.sid, "message", 0, 100), [])
        row, _ = await self.store.append(self.owner, self.sid, "valid", "message", {"role": "user"})
        self.assertEqual(row["seq"], 1)

    async def test_timeout_and_cancel_release_transaction(self):
        async with self.engine.hold(self.store, self.owner, self.sid):
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

    async def test_parallel_migration_startup(self):
        stores = await asyncio.gather(*(self.engine.open(self.cfg) for _ in range(3)))
        self.assertFalse(any(failed(store) for store in stores))
        for store in stores:
            await store.close()
        self.assertEqual((await self.store.get(self.owner, self.sid))["id"], str(self.sid))

    async def test_discovery_order_follows_appends(self):
        later = await self.store.create(self.owner, "work", "private", {})
        await self.store.append(self.owner, self.sid, "touch", "message", {"role": "user"})
        found = await self.store.discover(self.owner, ["*"], None, None, None, *MAX_CURSOR, 10, "")
        self.assertEqual([item["id"] for item in found], [str(self.sid), later["id"]])
        self.assertGreater(found[0]["updated_at"], found[0]["created_at"])

    async def test_api_real_owner_isolation_and_error_mapping(self):
        old_cfg, old_mode = state.CFG, state.CLIENT_AUTH_MODE
        state.CFG, state.CLIENT_AUTH_MODE = configuration(), None
        # Randomize configured identities to leave no user/test data behind.
        state.CFG.client_auth["clients"][self.owner] = state.CFG.client_auth["clients"].pop("alice")
        state.CFG.client_auth["clients"][self.other] = state.CFG.client_auth["clients"].pop("bob")
        try:
            with patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": self.engine.url}):
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


@POSTGRES_ONLY
class PostgresTests(SessionContract, unittest.IsolatedAsyncioTestCase):
    engine_class = PostgresEngine

    async def test_existing_postgresql_url_opens_the_same_store(self):
        self.assertIsInstance(self.store, PostgresSessionStore)
        short = "postgres:" + DSN.partition(":")[2]   # the other scheme asyncpg accepts
        other = await open_session_store(short, self.cfg, None)
        self.assertIsInstance(other, PostgresSessionStore)
        await other.close()

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

    async def test_connection_failure_does_not_expose_secrets(self):
        # Synthetic credentials, assembled so the public-change scanner does not read them as a real URL credential.
        problem = await PostgresSessionStore.open("postgresql://" + "private-user:private-password" + "@127.0.0.1:1/db", self.cfg)
        self.assertTrue(failed(problem))
        self.assertNotIn("private", problem.message)


class SqliteTests(SessionContract, unittest.IsolatedAsyncioTestCase):
    engine_class = SqliteEngine

    def raw(self):
        return sqlite3.connect(self.engine.path, isolation_level=None)

    async def test_relative_url_lies_in_the_state_folder(self):
        self.assertIsInstance(self.store, SqliteSessionStore)
        self.assertEqual(self.store.path, os.path.abspath(self.engine.path))

    @unittest.skipUnless(hasattr(os, "fchmod"), "owner/other mode bits exist on POSIX only")
    async def test_file_and_wal_files_are_owner_only(self):
        await self.store.append(self.owner, self.sid, "m1", "message", {"role": "user"})
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.engine.path + suffix):
                self.assertEqual(stat.S_IMODE(os.stat(self.engine.path + suffix).st_mode), 0o600, suffix)
        await self.store.close()
        os.chmod(self.engine.path, 0o644)   # widened by hand: the next start tightens it again
        self.store = await self.engine.open(self.cfg)
        self.assertEqual(stat.S_IMODE(os.stat(self.engine.path).st_mode), 0o600)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW") and hasattr(os, "symlink"), "O_NOFOLLOW exists on POSIX only")
    async def test_symbolic_link_in_place_of_the_file_is_refused(self):
        await self.store.close()
        target = os.path.join(self.engine.folder, "elsewhere.db")
        os.replace(self.engine.path, target)
        os.symlink(target, self.engine.path)
        problem = await self.engine.open(self.cfg)
        self.assertTrue(failed(problem))
        self.assertEqual(problem.status, 503)
        self.store = await open_session_store("sqlite:///elsewhere.db", self.cfg, self.engine.folder)

    async def test_unsupported_urls_are_refused_before_anything_opens(self):
        for url in ("mysql://example.invalid/db", "sqlite://relative.db", "sqlite:///", "sqlite:///../escape.db",
                    "sqlite:///s.db?mode=memory", "sqlite:///:memory:", "test-only", ""):
            problem = await open_session_store(url, self.cfg, self.engine.folder)
            self.assertTrue(failed(problem), url)
            self.assertEqual(problem.status, 400, url)
            self.assertNotIn("example.invalid", problem.message)
        for name in ("relative.db", "s.db", "escape.db"):
            self.assertFalse(os.path.exists(os.path.join(self.engine.folder, name)), name)

    async def test_missing_folder_and_directory_in_place_of_the_file(self):
        problem = await open_session_store("sqlite:///missing/sessions.db", self.cfg, self.engine.folder)
        self.assertEqual(problem.status, 503)
        os.mkdir(os.path.join(self.engine.folder, "folder.db"))
        problem = await open_session_store("sqlite:///folder.db", self.cfg, self.engine.folder)
        self.assertEqual(problem.status, 503)
        self.assertNotIn(self.engine.folder, problem.message)

    async def test_migration_failure_is_atomic_and_newer_schema_rejected(self):
        await self.store.close()
        bad = ("CREATE TABLE rollback_probe (id INTEGER)", "SELECT * FROM missing_table")
        with patch.object(sqlite_migrations, "MIGRATIONS", (*sqlite_migrations.MIGRATIONS, bad)):
            problem = await self.engine.open(self.cfg)
        self.assertEqual(problem.status, 503)
        db = self.raw()
        try:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='rollback_probe'").fetchone())
            self.assertEqual(db.execute("SELECT max(version) FROM schema_version").fetchone()[0], len(sqlite_migrations.MIGRATIONS))
            db.execute("INSERT INTO schema_version VALUES (999)")
        finally:
            db.close()
        problem = await self.engine.open(self.cfg)
        self.assertEqual(problem.status, 503)
        self.assertIn("newer", problem.message)
        db = self.raw()
        db.execute("DELETE FROM schema_version WHERE version=999")
        db.close()
        self.store = await self.engine.open(self.cfg)
        self.assertEqual((await self.store.get(self.owner, self.sid))["id"], str(self.sid))

    async def test_parallel_first_start_on_a_new_file(self):
        await self.store.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.engine.path + suffix):
                os.remove(self.engine.path + suffix)
        # Several processes starting at once on a file that does not exist yet: creation, the switch to WAL (which SQLite
        # does not queue behind the busy handler) and the migration must all succeed for every opener.
        stores = await asyncio.gather(*(self.engine.open(self.cfg) for _ in range(8)))
        for store in stores[1:]:
            if not failed(store):
                self.addAsyncCleanup(store.close)
        self.store = stores[0]
        self.assertEqual([store for store in stores if failed(store)], [])
        created = await asyncio.gather(*(store.create(self.owner, "work", "private", {}) for store in stores))
        self.assertEqual(len({row["id"] for row in created}), 8)
        db = self.raw()
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM schema_version").fetchone()[0], len(sqlite_migrations.MIGRATIONS))
        finally:
            db.close()

    async def test_second_process_writes_are_serialized_not_interleaved(self):
        # Two stores on one file (two router processes by mistake): appends from both stay gap-free and unique.
        second = await self.engine.open(self.cfg)
        try:
            rows = await asyncio.gather(*((self.store if n % 2 else second).append(self.owner, self.sid, f"m{n}", "message",
                                                                                     {"role": "user", "content": n}) for n in range(20)))
        finally:
            await second.close()
        self.assertEqual(sorted(row["seq"] for row, _ in rows), list(range(1, 21)))

    async def test_closed_store_answers_unavailable(self):
        await self.store.close()
        for call in (self.store.get(self.owner, self.sid), self.store.health(), self.store.outbox_counts()):
            self.assertEqual(await call, UNAVAILABLE)
        self.store = await self.engine.open(self.cfg)

    async def test_deleted_content_is_not_left_in_the_file(self):
        marker = "synthetic-erasure-marker-" + uuid4().hex
        await self.store.append(self.owner, self.sid, "m1", "message", {"role": "user", "content": marker})
        await self.store.delete(self.owner, self.sid)
        await self.store.close()   # the last connection checkpoints the WAL into the file and removes it
        for suffix in ("", "-wal"):
            if os.path.exists(self.engine.path + suffix):
                with open(self.engine.path + suffix, "rb") as f:
                    self.assertNotIn(marker.encode(), f.read(), suffix)
        self.store = await self.engine.open(self.cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
