#!/usr/bin/env python3
"""Real PostgreSQL tests of the deletion lifecycle (ADR-0076, TM-12) against a disposable database.

  * deleting a session with delete_memory_with_session marks the memory operations retained from it: never sent ->
    deleted without payload, possibly in Hindsight -> delete_pending, in flight -> taken over when the send finishes;
    without the option, and for another owner's or another session's operations, nothing changes;
  * the retention cleanup does the same with the option;
  * a restore of an older database is simulated (session and pending send back): replaying the journal deletes the
    session again and marks the send, so it is never delivered;
  * stuck deletions are counted; deleted documents are listed in pages for the Hindsight reconcile.

SKIRNIR_TEST_SESSION_DSN=postgresql://... python test/integration_deletion_lifecycle.py
Only synthetic, randomly scoped test owners are written. Never use production DBs.
"""
import os
import sys
import unittest
from uuid import UUID, uuid4

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router.session_config import SessionConfig  # noqa: E402
from skirnir_router.session_store import PostgresSessionStore  # noqa: E402
from skirnir_router.session_types import NOT_FOUND  # noqa: E402

DSN = os.environ.get("SKIRNIR_TEST_SESSION_DSN")
BANK = "skirnir-test-" + "0" * 40


@unittest.skipUnless(DSN, "set SKIRNIR_TEST_SESSION_DSN to a disposable PostgreSQL database")
class DeletionLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = await PostgresSessionStore.open(DSN, SessionConfig(enabled=True))
        self.owner = "test-" + uuid4().hex
        self.other = "test-" + uuid4().hex

    async def asyncTearDown(self):
        try:
            async with self.store.pool.acquire() as conn:
                await conn.execute("DELETE FROM skirnir_sessions.memory_outbox WHERE owner=ANY($1::text[])", [self.owner, self.other])
                await conn.execute("DELETE FROM skirnir_sessions.sessions WHERE owner=ANY($1::text[])", [self.owner, self.other])
        finally:
            await self.store.close()

    async def session(self, owner=None):
        s = await self.store.create(owner or self.owner, "work", "private", {})
        return UUID(s["id"])

    async def op(self, session_id, owner=None, state="pending", attempts=0):
        owner = owner or self.owner
        operation = uuid4()
        scope = {"principal": owner, "project": "work", "__bank": BANK}
        row = await self.store.outbox_put(operation, scope, {"content": "synthetic"}, "f-" + operation.hex, 1000,
                                          session_id=session_id)
        self.assertEqual(row["state"], "pending")
        if state != "pending" or attempts:
            async with self.store.pool.acquire() as conn:
                await conn.execute("UPDATE skirnir_sessions.memory_outbox SET state=$2,attempts=$3 WHERE operation_id=$1",
                                   operation, state, attempts)
        return operation

    async def row(self, operation):
        async with self.store.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT state,payload,delete_requested FROM skirnir_sessions.memory_outbox WHERE operation_id=$1", operation)
        return r["state"], r["payload"], r["delete_requested"]

    async def test_session_delete_with_memory(self):
        sid, keep = await self.session(), await self.session()
        unsent, sent, other_session = await self.op(sid), await self.op(sid, state="accepted", attempts=1), await self.op(keep)
        foreign = await self.op(sid, owner=self.other)   # same session id under another owner: not this owner's data
        self.assertIsNone(await self.store.delete(self.owner, sid, with_memory=True))
        self.assertEqual((await self.row(unsent))[:2], ("deleted", None))
        self.assertEqual((await self.row(sent))[0], "delete_pending")
        self.assertEqual((await self.row(other_session))[0], "pending")
        self.assertEqual((await self.row(foreign))[0], "pending")
        self.assertEqual(await self.store.delete(self.owner, sid, with_memory=True), NOT_FOUND)

    async def test_session_delete_without_option_keeps_memory(self):
        sid = await self.session()
        unsent = await self.op(sid)
        self.assertIsNone(await self.store.delete(self.owner, sid))
        self.assertEqual((await self.row(unsent))[0], "pending")

    async def test_deletion_requested_in_flight_takes_over(self):
        sid = await self.session()
        operation = await self.op(sid)
        claimed = None
        for _ in range(50):   # other test rows may be due first; claim until ours
            row = await self.store.outbox_claim(60, 3)
            if row is None:
                break
            if UUID(row["operation_id"]) == operation:
                claimed = row
                break
            await self.store.outbox_finish(UUID(row["operation_id"]), UUID(row["lease"]), "accepted")
        self.assertIsNotNone(claimed)
        self.assertIsNone(await self.store.delete(self.owner, sid, with_memory=True))
        self.assertEqual(await self.row(operation), ("processing", {"content": "synthetic"}, True))
        self.assertIsNone(await self.store.outbox_finish(operation, UUID(claimed["lease"]), "accepted"))
        self.assertEqual((await self.row(operation))[0], "delete_pending")

    async def test_retention_cleanup_with_memory(self):
        sid = await self.session()
        unsent = await self.op(sid)
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.sessions SET updated_at=now()-interval '40 days' WHERE id=$1", sid)
        result = await self.store.cleanup(30, 0, 0, with_memory=True)
        self.assertGreaterEqual(result["memory_deletions"], 1)
        self.assertEqual((await self.row(unsent))[0], "deleted")

    async def test_journal_replay_after_restore(self):
        sid = await self.session()
        unsent = await self.op(sid)
        sent = await self.op(None, state="accepted", attempts=1)
        async with self.store.pool.acquire() as conn:
            snapshot = await conn.fetch("SELECT * FROM skirnir_sessions.memory_outbox WHERE owner=$1", self.owner)
            session_row = await conn.fetchrow("SELECT * FROM skirnir_sessions.sessions WHERE id=$1", sid)
        self.assertIsNone(await self.store.delete(self.owner, sid, with_memory=True))
        # "restore": the old database state comes back - the session and the pending send with its content
        async with self.store.pool.acquire() as conn:
            cols = list(session_row.keys())
            await conn.execute(f"INSERT INTO skirnir_sessions.sessions({','.join(cols)}) VALUES({','.join(f'${i + 1}' for i in range(len(cols)))})",
                               *[session_row[c] for c in cols])
            for r in snapshot:
                await conn.execute("UPDATE skirnir_sessions.memory_outbox SET state=$2,payload=$3,delete_requested=$4 WHERE operation_id=$1",
                                   r["operation_id"], r["state"], r["payload"], r["delete_requested"])
        self.assertEqual((await self.row(unsent))[0], "pending")
        result = await self.store.replay_deletions([(self.owner, sid)], [(self.owner, sent)], True)
        self.assertEqual(result, {"sessions": 1, "operations": 2})
        self.assertEqual(await self.store.get(self.owner, sid), NOT_FOUND)
        self.assertEqual((await self.row(unsent))[:2], ("deleted", None))
        self.assertEqual((await self.row(sent))[0], "delete_pending")
        again = await self.store.replay_deletions([(self.owner, sid)], [(self.owner, sent)], True)
        self.assertEqual(again, {"sessions": 0, "operations": 0})

    async def test_stuck_and_reconcile_listing(self):
        before = await self.store.outbox_stuck_deletions()
        dead = await self.op(None, state="dead", attempts=3)
        async with self.store.pool.acquire() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET delete_requested=true WHERE operation_id=$1", dead)
        self.assertEqual(await self.store.outbox_stuck_deletions(), before + 1)
        gone = sorted([await self.op(None, state="deleted", attempts=1) for _ in range(3)])
        never_sent = await self.op(None, state="deleted", attempts=0)
        seen, after = [], UUID(int=0)
        while True:
            page = await self.store.outbox_deleted_documents(after, 2)
            seen += [op for op, bank in page if op in gone or op == never_sent]
            self.assertTrue(all(bank for _, bank in page))
            if len(page) < 2:
                break
            after = page[-1][0]
        self.assertEqual(sorted(seen), gone)   # sent and deleted only; a never-sent row has nothing in Hindsight


if __name__ == "__main__":
    unittest.main(verbosity=2)
