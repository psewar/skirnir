#!/usr/bin/env python3
"""ADR-0145 (TM-10): writes into a reviewed shared memory scope wait for a reviewer before they reach the bank.

Configuration rules, and the whole path through the router's HTTP API with a real session store: SQLite always (a file in
a temporary folder), PostgreSQL when SKIRNIR_TEST_SESSION_DSN points to a disposable database. The memory backend is the
synthetic loopback Hindsight of selftest_memory, which records every delivery (`self.operations`). No network."""
import asyncio
import copy
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import UUID, uuid4

from selftest_memory import BASE, MemoryHarness, PROJECT, policy
import selftest_interoperability as shared_fixture
from selftest_interoperability import SHARED
from store_engines import POSTGRES_ONLY, PostgresEngine, SqliteEngine, prepare
from skirnir_router import config, context_ops, memory, memory_gateway, memory_outbox, sqlite_migrations, state
from skirnir_router.outcome import failed
from skirnir_router.session_config import SessionConfig
from skirnir_router.shared_config import parse_scopes

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import memory_review as review_tool  # noqa: E402

TEAM = "example-team"


class ConfigTests(unittest.TestCase):
    def scope(self, **extra):
        return {TEAM: {"data_class": "personal", "allow_cloud": False, "writers": ["example-writer"], **extra}}

    def test_review_settings_are_parsed_with_defaults_and_bounds(self):
        parsed = parse_scopes(self.scope(), ["personal"])[TEAM]
        self.assertEqual((parsed["review"], parsed["reviewers"]), (False, ()))   # off by default: existing scopes unchanged
        parsed = parse_scopes(self.scope(review=True, reviewers=["example-reviewer"]), ["personal"])[TEAM]
        self.assertEqual((parsed["review"], parsed["reviewers"], parsed["review_expire_days"], parsed["review_max_held"]),
                         (True, ("example-reviewer",), 14, 100))
        refused = (dict(review="yes", reviewers=["r"]), dict(review=True), dict(review=True, reviewers=[]),
                   dict(review=True, reviewers=["r", "r"]), dict(review=True, reviewers="r"),
                   dict(reviewers=["r"]), dict(review_expire_days=14), dict(review=False, review_max_held=5),
                   dict(review=True, reviewers=["r"], review_expire_days=0), dict(review=True, reviewers=["r"], review_expire_days=366),
                   dict(review=True, reviewers=["r"], review_expire_days=True), dict(review=True, reviewers=["r"], review_max_held=0),
                   dict(review=True, reviewers=["r"], review_max_held=10001), dict(review=True, reviewers=["r"], reviewed=True))
        for extra in refused:
            with self.subTest(extra=extra):
                self.assertTrue(failed(parse_scopes(self.scope(**extra), ["personal"])))

    def test_loading_needs_known_reviewers_with_read_and_the_durable_outbox(self):
        raw = copy.deepcopy(config.load_yaml(BASE))
        writer, reader, stranger = policy(), policy(write=False), policy(write=False)
        for spec in (writer, reader):
            spec["projects"][PROJECT]["scope"] = TEAM
        raw["router"]["client_auth"] = {"clients": {"example-writer": {"memory": writer}, "example-reviewer": {"memory": reader},
                                                    "example-stranger": {"memory": stranger}}}

        def load(reviewers, outbox):
            raw["router"]["hindsight"] = {"enabled": outbox, "outbox_enabled": outbox}
            raw["router"]["session_store"] = {"enabled": outbox, "dsn_env": "SKIRNIR_SESSION_DSN"}
            raw["router"]["memory_scopes"] = {TEAM: {"data_class": "personal", "writers": ["example-writer"], "review": True,
                                                     "reviewers": reviewers}}
            self.assertEqual(config.validate_schema(raw), [])
            return config.Config.__new__(config.Config).parse(copy.deepcopy(raw))

        self.assertFalse(failed(result := load(["example-reviewer"], True)), getattr(result, "message", result))
        for reviewers, outbox, reason in ((["example-reviewer"], False, "outbox_enabled"), (["example-unknown"], True, "unknown client"),
                                          (["example-stranger"], True, "needs a project grant with read")):
            with self.subTest(reviewers=reviewers, outbox=outbox):
                result = load(reviewers, outbox)
                self.assertTrue(failed(result))
                self.assertIn(reason, result.message)

    def test_the_tool_refuses_missing_settings_and_clear_text_to_another_host(self):
        for environ, reason in (({}, "SKIRNIR_REVIEW_URL"), ({"SKIRNIR_REVIEW_URL": "https://router.example"}, "SKIRNIR_REVIEW_TOKEN"),
                                ({"SKIRNIR_REVIEW_URL": "http://router.example:11434", "SKIRNIR_REVIEW_TOKEN": "t"}, "plain http"),
                                ({"SKIRNIR_REVIEW_URL": "https://user@router.example", "SKIRNIR_REVIEW_TOKEN": "t"}, "credentials"),
                                ({"SKIRNIR_REVIEW_URL": "https://router.example", "SKIRNIR_REVIEW_TOKEN": "t",
                                  "SKIRNIR_REVIEW_CA": os.path.join(tempfile.gettempdir(), "missing-" + uuid4().hex)}, "CA")):
            with self.subTest(environ=environ):
                out = io.StringIO()
                self.assertEqual(review_tool.main(["approve", str(uuid4())], environ, out), 2)
                self.assertIn(reason, out.getvalue())
        for host, ok in (("127.0.0.1", True), ("127.9.9.9", True), ("::1", True), ("localhost", True), ("192.0.2.1", False),
                         ("router.example", False), ("999.0.0.1", False)):
            with self.subTest(host=host):
                self.assertEqual(review_tool.loopback(host), ok)


class SqliteMigrationTests(unittest.TestCase):
    def test_migration_2_keeps_rows_and_counts_and_accepts_the_review_states(self):
        folder = tempfile.mkdtemp(prefix="skirnir-review-migration-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        db = sqlite3.connect(os.path.join(folder, "sessions.db"), isolation_level=None)
        self.addCleanup(db.close)
        db.execute("BEGIN IMMEDIATE")
        with patch.object(sqlite_migrations, "MIGRATIONS", sqlite_migrations.MIGRATIONS[:1]):
            self.assertIsNone(sqlite_migrations.migrate(db))
        db.execute("""INSERT INTO memory_outbox(operation_id,owner,project,scope,fingerprint,payload,state,available_at,created_at,updated_at)
                      VALUES('a','example-owner','p','{}','f','{"content":"Synthetic"}','pending',1,1,1)""")
        db.execute("COMMIT")
        db.execute("BEGIN IMMEDIATE")
        self.assertIsNone(sqlite_migrations.migrate(db))
        db.execute("COMMIT")
        self.assertEqual(db.execute("SELECT owner,state,payload FROM memory_outbox").fetchall(),
                         [("example-owner", "pending", '{"content":"Synthetic"}')])
        db.execute("UPDATE memory_outbox SET state='awaiting_review'")
        db.execute("UPDATE memory_outbox SET state='rejected'")
        totals = dict(db.execute("SELECT state,jobs FROM outbox_totals").fetchall())
        self.assertEqual((totals["pending"], totals["awaiting_review"], totals["rejected"]), (0, 0, 1))
        with self.assertRaises(sqlite3.IntegrityError):   # the CHECK constraint still refuses an unknown state
            db.execute("UPDATE memory_outbox SET state='unknown'")


class ReviewHttpContract:
    """client-a and client-b write the shared scope; client-owned reviews it (and may write it too, then unheld)."""

    async def asyncSetUp(self):
        prepare(self)
        await super().asyncSetUp()
        shared_fixture.SharedGatewayTests.shared(self)
        self.service.worker.cancel()
        await asyncio.gather(self.service.worker, return_exceptions=True)
        self.service.worker = None
        self.real = await self.engine.open(SessionConfig(enabled=True))
        self.service.outbox = self.real
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_enabled=True)
        state.CFG.memory_scopes[TEAM].update(review=True, reviewers=("client-owned",), review_expire_days=14, review_max_held=100)
        reviewer = policy(owner="client", write=False)
        reviewer.update(strategy="manual")
        reviewer["projects"][SHARED] = {"scope": TEAM, "data_class": "personal", "read": True, "write": False}
        state.CFG.client_auth["clients"]["client-owned"]["memory"] = reviewer

    async def asyncTearDown(self):
        async with self.real.connection() as conn:
            await conn.execute("DELETE FROM skirnir_sessions.memory_outbox WHERE owner=ANY($1::text[])",
                               ["client-a", "client-b", "client-owned"])
        await super().asyncTearDown()
        await self.real.close()

    async def request(self, path, payload=None, token="client-a", method="post"):
        return await getattr(self.client, method)(path, json=payload, headers={"Authorization": "Bearer " + token})

    async def retain(self, event="one", token="client-a"):
        response = await self.request(memory.PREFIX + "/retain", {"project": SHARED, "event_id": event,
                                                                  "content": "Synthetic shared knowledge " + event, "confirmed": True}, token)
        return response.status, await response.json()

    async def held(self, token="client-owned", project=SHARED):
        response = await self.request(memory.PREFIX + "/review", {"project": project}, token)
        return response.status, await response.json()

    async def decide(self, operation, decision, token="client-owned"):
        response = await self.request(memory.PREFIX + f"/operations/{operation}/{decision}", {}, token)
        return response.status, await response.json()

    async def state_of(self, operation, owner="client-a"):
        return await self.real.outbox_get(owner, UUID(operation))

    async def test_a_writers_retain_waits_and_is_delivered_only_after_approval(self):
        status, value = await self.retain()
        self.assertEqual((status, value["state"]), (202, "awaiting_review"))
        operation = value["operation_id"]
        self.assertFalse(await memory_outbox.step(self.service))   # the worker never claims a held write
        self.assertEqual(self.operations, {})
        own = await self.request(memory.PREFIX + "/operations/" + operation, token="client-a", method="get")
        self.assertEqual((await own.json())["state"], "awaiting_review")   # the writer sees the state as a fact
        gateway = await self.request(memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/operations/" + operation, method="get")
        self.assertEqual((await gateway.json())["status"], "pending")
        self.assertEqual(await self.real.outbox_held_counts(), {TEAM: 1})   # what /admin/context shows: a count per scope
        status, page = await self.held()
        self.assertEqual(status, 200)
        self.assertEqual([(i["operation_id"], i["writer"], i["content"]) for i in page["items"]],
                         [(operation, "client-a", "Synthetic shared knowledge one")])
        self.assertEqual(page["items"][0]["provenance"]["origin_client"], "client-a")
        status, value = await self.decide(operation, "approve")
        self.assertEqual((status, value["state"]), (200, "pending"))
        self.assertTrue(await memory_outbox.step(self.service))
        self.assertEqual(len(self.operations), 1)
        self.assertEqual((await self.state_of(operation))["state"], "accepted")
        self.assertEqual((await self.decide(operation, "approve"))[1]["reason"], "not_awaiting_review")

    async def test_a_reviewers_own_write_is_not_held(self):
        spec = state.CFG.client_auth["clients"]["client-owned"]["memory"]["projects"][SHARED]
        spec["write"] = True
        state.CFG.memory_scopes[TEAM]["writers"] = ("client-a", "client-b", "client-owned")
        status, value = await self.retain(token="client-owned")
        self.assertEqual((status, value["state"]), (202, "pending"))
        self.assertTrue(await memory_outbox.step(self.service))
        self.assertEqual(len(self.operations), 1)

    async def test_rejection_and_expiry_never_deliver_and_drop_the_text(self):
        first = (await self.retain("first"))[1]["operation_id"]
        second = (await self.retain("second"))[1]["operation_id"]
        status, value = await self.decide(first, "reject")
        self.assertEqual((status, value["state"], value["reason"]), (200, "rejected", "review_rejected"))
        row = await self.state_of(first)
        self.assertEqual((row["state"], row["payload"]), ("rejected", None))
        self.assertEqual((await self.retain("first"))[1]["state"], "rejected")   # a replay cannot bring it back
        self.assertEqual((await self.retain("third"))[1]["state"], "awaiting_review")   # a new event is held again
        await context_ops.expire_reviews(self.real)
        self.assertEqual((await self.state_of(second))["state"], "awaiting_review")   # deadline not reached
        async with self.real.connection() as conn:
            await conn.execute("UPDATE skirnir_sessions.memory_outbox SET available_at=clock_timestamp() WHERE operation_id=$1", UUID(second))
        await context_ops.expire_reviews(self.real)
        row = await self.state_of(second)
        self.assertEqual((row["state"], row["last_error"], row["payload"]), ("rejected", "review_expired", None))
        self.assertFalse(await memory_outbox.step(self.service))
        self.assertEqual(self.operations, {})

    async def test_only_a_reviewer_of_the_scope_sees_or_decides_a_held_write(self):
        operation = (await self.retain())[1]["operation_id"]
        for token in ("client-a", "client-b"):   # its own writer and another writer of the scope
            for decision in ("approve", "reject"):
                with self.subTest(token=token, decision=decision):
                    self.assertEqual((await self.decide(operation, decision, token))[0], 404)
            self.assertEqual((await self.held(token))[0], 403)
        # A reviewer of another reviewed scope, with a read grant there only.
        state.CFG.memory_scopes["other-team"] = {"data_class": "personal", "allow_cloud": False, "writers": (), "review": True,
                                                 "reviewers": ("client-owned",), "review_expire_days": 14, "review_max_held": 100}
        projects = state.CFG.client_auth["clients"]["client-owned"]["memory"]["projects"]
        projects["other"] = {**projects.pop(SHARED), "scope": "other-team"}
        self.assertEqual((await self.decide(operation, "approve"))[0], 404)
        self.assertEqual((await self.held(project="other"))[1]["items"], [])
        self.assertEqual((await self.decide(str(uuid4()), "approve"))[0], 404)
        self.assertEqual((await self.request(memory.PREFIX + f"/operations/{operation}/approve", {}, "client-unknown")).status, 401)
        self.assertEqual((await self.state_of(operation))["state"], "awaiting_review")
        self.assertEqual(self.operations, {})

    async def test_a_writer_that_lost_its_right_is_revoked_on_approval(self):
        operation = (await self.retain())[1]["operation_id"]
        state.CFG.memory_scopes[TEAM]["writers"] = ("client-b",)
        status, value = await self.decide(operation, "approve")
        self.assertEqual((status, value["state"], value["reason"]), (200, "revoked", "policy_changed"))
        self.assertIsNone((await self.state_of(operation))["payload"])
        self.assertFalse(await memory_outbox.step(self.service))
        self.assertEqual(self.operations, {})

    async def test_held_writes_are_bounded_per_writer_and_scope(self):
        state.CFG.memory_scopes[TEAM]["review_max_held"] = 2
        for event in ("one", "two"):
            self.assertEqual((await self.retain(event))[0], 202)
        status, value = await self.retain("three")
        self.assertEqual(status, 429)
        self.assertIn("review_max_held", value["error"]["message"])
        self.assertEqual((await self.retain("four", token="client-b"))[1]["state"], "awaiting_review")   # another writer
        # Held writes do not fill the delivery capacity: a client of a private bank is still admitted.
        self.service.cfg = state.CFG.hindsight = replace(self.service.cfg, outbox_capacity=1)
        response = await self.request(memory.PREFIX + "/retain", {"project": PROJECT, "event_id": "private", "content": "Synthetic",
                                                                  "confirmed": True})
        self.assertEqual(response.status, 202)

    async def test_gateway_and_automatic_retention_are_held_and_the_writer_may_withdraw(self):
        base = memory_gateway.GATEWAY + "/v1/default/banks/" + SHARED + "/memories"
        response = await self.request(base, {"items": [{"content": "Synthetic gateway knowledge", "document_id": "doc-1"}],
                                             "async": True})
        self.assertIn(response.status, (200, 202), await response.text())
        # A synchronous retain does not wait for a reviewer (that may take days): admitted at once, with the state.
        response = await self.request(base, {"items": [{"content": "Synthetic synchronous knowledge"}], "async": False})
        self.assertEqual(response.status, 202, await response.text())
        self.assertEqual((await response.json())["state"], "awaiting_review")
        spec = state.CFG.client_auth["clients"]["client-b"]["memory"]
        spec.update(strategy="auto", default_project=SHARED, auto_retain=True)
        response = await self.infer(enabled=False, token="client-b", extra={"routing": {"request_id": "auto-retain"}})
        self.assertEqual(response.status, 200, await response.text())
        counts = await self.real.outbox_counts()
        self.assertEqual((counts.get("awaiting_review"), counts.get("pending")), (3, None))
        self.assertFalse(await memory_outbox.step(self.service))
        page = (await self.held())[1]["items"]
        self.assertEqual([i["writer"] for i in page], ["client-a", "client-a", "client-b"])
        withdrawn = await self.request(memory.PREFIX + "/operations/" + page[0]["operation_id"], method="delete")
        self.assertEqual(((await withdrawn.json())["state"]), "cancelled")
        self.assertEqual([i["writer"] for i in (await self.held())[1]["items"]], ["client-a", "client-b"])

    async def test_a_held_write_survives_a_restart(self):
        operation = (await self.retain())[1]["operation_id"]
        await self.real.close()
        self.real = await self.engine.open(SessionConfig(enabled=True))
        self.service.outbox = self.real
        self.assertEqual((await self.state_of(operation))["state"], "awaiting_review")
        self.assertFalse(await memory_outbox.step(self.service))

    async def test_the_review_tool_lists_and_approves(self):
        operation = (await self.retain())[1]["operation_id"]
        environ = {"SKIRNIR_REVIEW_URL": str(self.client.make_url("")).rstrip("/"), "SKIRNIR_REVIEW_TOKEN": "client-owned"}

        def run(*argv):
            out = io.StringIO()
            return review_tool.main(list(argv), environ, out), out.getvalue()

        code, text = await asyncio.to_thread(run, "list", "--project", SHARED)
        self.assertEqual(code, 0, text)
        self.assertIn(operation, text)
        self.assertIn("Synthetic shared knowledge one", text)
        code, text = await asyncio.to_thread(run, "show", "--project", SHARED, operation)
        self.assertEqual(code, 0, text)
        self.assertIn('"writer": "client-a"', text)
        environ["SKIRNIR_REVIEW_TOKEN"] = "client-a"
        code, text = await asyncio.to_thread(run, "approve", operation)
        self.assertEqual((code, "HTTP 404" in text), (1, True), text)   # a writer cannot release its own write
        environ["SKIRNIR_REVIEW_TOKEN"] = "client-owned"
        code, text = await asyncio.to_thread(run, "approve", operation)
        self.assertEqual((code, text.strip()), (0, f"{operation}: pending"))
        self.assertTrue(await memory_outbox.step(self.service))
        self.assertEqual(len(self.operations), 1)


@POSTGRES_ONLY
class ReviewHttpPostgresTests(ReviewHttpContract, MemoryHarness):
    engine_class = PostgresEngine


class ReviewHttpSqliteTests(ReviewHttpContract, MemoryHarness):
    engine_class = SqliteEngine


if __name__ == "__main__":
    unittest.main()
