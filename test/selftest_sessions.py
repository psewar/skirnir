#!/usr/bin/env python3
"""Session API/config unit tests, store URL parsing and the SQLite statement translation; no database needed.

Run: python test/selftest_sessions.py
"""
import asyncio
import copy
import hashlib
import inspect
import io
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import app, auth, config, session_api, state  # noqa: E402
from skirnir_router.session_config import SessionConfig, parse_session_config  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402
from skirnir_router import sqlite_store  # noqa: E402
from skirnir_router.session_store import OutboxStore, PostgresSessionStore, SessionStore  # noqa: E402
from skirnir_router.session_types import NOT_FOUND, UNAVAILABLE  # noqa: E402
from skirnir_router.session_url import StoreURL, parse_store_url  # noqa: E402
from skirnir_router.sqlite_store import SqliteSessionStore, translate  # noqa: E402

BASE = os.path.join(os.path.dirname(__file__), "..", "router", "config.example.yaml")
URL = session_api.PREFIX


def configuration(enabled=True):
    cfg = config.Config(BASE, use_overrides=False)
    cfg.session_store = SessionConfig(enabled=enabled)
    cfg.client_auth = {"mode": "observe", "audit_log": None, "clients": {
        name: {"token_sha256": hashlib.sha256(name.encode()).hexdigest(), "session_projects": projects}
        for name, projects in (("alice", ["work"]), ("bob", ["work"]), ("denied", []))
    }}
    return cfg


class ConfigTests(unittest.TestCase):
    def test_defaults_and_strict_validation(self):
        self.assertFalse(configuration(False).session_store.enabled)
        for raw in ({"enabled": "false"}, {"enabled": 1}, {"timeout_s": True}, {"timeout_s": float("nan")},
                    {"timeout_s": 61}, {"dsn_env": "postgres://secret"}, {"min_pool_size": 0}, {"max_pool_size": "5"},
                    {"min_pool_size": 6, "max_pool_size": 5}, {"unknown": True}, []):
            with self.subTest(raw=raw):
                self.assertTrue(failed(parse_session_config(raw)))

    def test_schema_and_client_projects(self):
        raw = copy.deepcopy(config.load_yaml(BASE))
        raw["router"]["session_store"] = {"enabled": False, "dsn_env": "MY_DSN"}
        raw["router"]["client_auth"] = {"clients": {"a": {"session_projects": ["work"]}}}
        cfg = config.Config.__new__(config.Config)
        self.assertIsNone(cfg.parse(raw))
        self.assertEqual(cfg.session_store.dsn_env, "MY_DSN")
        raw["router"]["client_auth"]["clients"]["a"]["session_projects"] = "*"
        self.assertTrue(failed(cfg.parse(raw)))
        raw["router"]["session_store"]["dsn"] = "secret"
        self.assertTrue(config.validate_schema(raw))

    def test_no_driver_import_when_disabled(self):
        # Importing/building an ordinary router must never import asyncpg.
        import subprocess
        subprocess.run([sys.executable, "-c", "from skirnir_router import app; import sys; assert 'asyncpg' not in sys.modules"],
                       env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(BASE))}, check=True)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_cfg = state.CFG
        self.old_mode = state.CLIENT_AUTH_MODE
        state.CFG = configuration()
        state.CLIENT_AUTH_MODE = None
        self.store = AsyncMock(spec=SessionStore)
        self.sid = str(uuid4())
        self.session = {"id": self.sid, "owner": "alice", "project": "work", "mode": "client-managed", "status": "active"}
        self.store.create.return_value = self.session
        self.store.get.return_value = self.session
        self.store.update.return_value = self.session
        self.store.append.return_value = ({"seq": 1, "event_id": "one"}, True)
        self.store.read.return_value = [{"seq": 1}]
        self.open_patch = patch.object(session_api.PostgresSessionStore, "open", return_value=self.store)
        self.open_mock = self.open_patch.start()
        self.env_patch = patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": "postgresql://test-only"})
        self.env_patch.start()
        api, _ = app.build_apps()
        self.client = TestClient(TestServer(api))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.env_patch.stop()
        self.open_patch.stop()
        state.CFG, state.CLIENT_AUTH_MODE = self.old_cfg, self.old_mode

    async def request(self, method="GET", path=None, token="alice", **kwargs):
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        return await self.client.request(method, path or URL + "/" + self.sid, headers=headers, **kwargs)

    # TM-09: every registered session route, found from the router itself, so a route added later is covered too.
    def session_routes(self):
        api, _ = app.build_apps()
        routes = set()
        for route in api.router.routes():
            info = route.resource.get_info() if route.resource is not None else {}
            template = info.get("formatter") or info.get("path") or ""
            if template.startswith(URL) and route.method not in ("HEAD", "OPTIONS"):
                routes.add((route.method, template))
        return sorted(routes)

    def owner_arguments(self):
        """The owner each owner-scoped store call received (first parameter named owner)."""
        seen = []
        for name, args, kwargs in self.store.mock_calls:
            method = getattr(SessionStore, name.split(".")[0], None)
            params = list(inspect.signature(method).parameters)[1:] if method else []
            if params and params[0] == "owner":
                seen.append((name, kwargs.get("owner", args[0] if args else None)))
        return seen

    async def test_every_session_route_requires_identity(self):
        routes = self.session_routes()
        self.assertGreaterEqual(len(routes), 15, routes)
        for method, template in routes:
            with self.subTest(route=f"{method} {template}"):
                response = await self.request(method, template.replace("{session_id}", self.sid), token=None, json={})
                self.assertEqual(response.status, 401)
        # health() comes from the store lifecycle, not from these requests; no owner-scoped call may happen at all.
        self.assertEqual(self.owner_arguments(), [], "no owner-scoped store access without an identity")

    async def test_every_session_route_scopes_store_access_to_the_caller(self):
        # bob is a valid client of the same project; the session belongs to alice, so the store finds nothing for bob.
        for method_name in ("get", "update", "delete", "read", "context", "compactions", "search"):
            getattr(self.store, method_name).return_value = NOT_FOUND
        self.store.append.return_value = NOT_FOUND
        self.store.discover.return_value = []
        for method, template in self.session_routes():
            with self.subTest(route=f"{method} {template}"):
                self.store.reset_mock()
                response = await self.request(method, template.replace("{session_id}", self.sid), token="bob",
                                              json={"project": "work", "status": "archived"})
                self.assertFalse(200 <= response.status < 300 and "{session_id}" in template,
                                 f"{method} {template} answered {response.status} for a session of another owner")
                self.assertTrue(all(owner == "bob" for _, owner in self.owner_arguments()), self.owner_arguments())

    async def test_create_explicit_and_no_owner_spoof(self):
        response = await self.request("POST", URL, json={"project": "work", "metadata": {"name": "task"}})
        self.assertEqual(response.status, 201)
        self.store.create.assert_awaited_once_with("alice", "work", state.CFG.cloud["default_data_class"], {"name": "task"})
        for extra in ({"owner": "bob"}, {"mode": "skirnir-managed"}, {"metadata": []}):
            response = await self.request("POST", URL, json={"project": "work", **extra})
            self.assertEqual(response.status, 400)
        response = await self.request("POST", URL, json={"project": "other"})
        self.assertEqual(response.status, 403)

    async def test_auth_even_observe_and_ip_only(self):
        for token, status in ((None, 401), ("wrong", 401), ("denied", 403)):
            self.assertEqual((await self.request(token=token)).status, status)
        state.CFG.client_auth["clients"]["alice"]["ip"] = ["127.0.0.1"]
        self.assertEqual((await self.request(token=None)).status, 401)
        self.store.get.assert_not_awaited()

    async def test_auth_enforce_and_other_credential_forms(self):
        import base64
        state.CFG.client_auth["mode"] = "enforce"
        self.assertEqual((await self.request(token="wrong")).status, 401)
        for headers in ({"x-api-key": "alice"}, {"Authorization": "Basic " + base64.b64encode(b"alice:alice").decode()}):
            response = await self.client.get(URL + "/" + self.sid, headers=headers)
            self.assertEqual(response.status, 200)

    async def test_no_auth_config_is_not_public_session_access(self):
        state.CFG.client_auth["clients"] = {}
        self.assertEqual((await self.request(token=None)).status, 401)

    async def test_owner_is_always_derived_from_credentials(self):
        self.store.get.return_value = NOT_FOUND
        response = await self.request(token="bob")
        self.assertEqual(response.status, 404)
        self.store.get.assert_awaited_once_with("bob", session_api.UUID(self.sid))
        self.assertEqual((await self.request(path=URL + "/not-a-uuid")).status, 404)

    async def test_revoked_project(self):
        state.CFG.client_auth["clients"]["alice"]["session_projects"] = ["another"]
        for method, suffix in (("GET", ""), ("DELETE", ""), ("POST", "/messages"), ("PATCH", "")):
            self.assertEqual((await self.request(method, URL + "/" + self.sid + suffix)).status, 404)
        self.store.delete.assert_not_awaited()
        self.store.append.assert_not_awaited()

    async def test_append_keeps_protocol_fields_and_idempotent_result(self):
        msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "function": {"name": "search", "arguments": "{}"}}]}
        response = await self.request("POST", URL + "/" + self.sid + "/messages", json={"event_id": "one", "message": msg})
        self.assertEqual(response.status, 201)
        self.assertEqual(self.store.append.await_args.args, ("alice", session_api.UUID(self.sid), "one", "message", msg))
        self.store.append.return_value = ({"seq": 1}, False)
        self.assertEqual((await self.request("POST", URL + "/" + self.sid + "/messages",
                                            json={"event_id": "one", "message": msg})).status, 200)

    async def test_checkpoint_validation(self):
        path = URL + "/" + self.sid + "/checkpoints"
        good = {"schema_version": 1, "goals": ["finish"], "open_steps": [], "artifacts": ["repo:file"],
                "last_result": "tested", "provenance": {"client": "hermes"}}
        self.assertEqual((await self.request("POST", path, json={"event_id": "c1", "checkpoint": good})).status, 201)
        for checkpoint in ({}, {"schema_version": True}, {"schema_version": 2}, {**good, "goals": "bad"}, {**good, "process": 123}):
            self.assertEqual((await self.request("POST", path, json={"event_id": "c1", "checkpoint": checkpoint})).status, 400)

    async def test_pagination(self):
        response = await self.request(path=URL + "/" + self.sid + "/messages?after=0&limit=1")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"items": [{"seq": 1}], "next_after": 1})
        for query in ("after=-1", "limit=101", "limit=x", "after=9223372036854775808", "owner=bob"):
            self.assertEqual((await self.request(path=URL + "/" + self.sid + "/messages?" + query)).status, 400)

    async def test_update_delete_and_store_failure(self):
        self.assertEqual((await self.request("PATCH", json={"status": "archived"})).status, 200)
        self.assertEqual((await self.request("DELETE")).status, 204)
        self.store.get.return_value = UNAVAILABLE
        response = await self.request()
        self.assertEqual(response.status, 503)
        self.assertNotIn("secret", await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_invalid_json_and_body_limit(self):
        for raw in (b"[]", b"{bad", b'{"metadata":NaN}', b'{"a":', b'\xff'):
            self.assertEqual((await self.request("POST", URL, data=raw)).status, 400)
        self.assertEqual((await self.request("POST", URL, data=io.BytesIO(b"x" * (session_api.MAX_BODY_BYTES + 1)))).status, 413)

    async def test_cancellation_not_swallowed(self):
        self.store.get.side_effect = asyncio.CancelledError
        # Direct dispatch verifies cancellation propagates instead of producing success.
        request = unittest.mock.MagicMock()
        request.get.return_value = "alice"
        request.app = {session_api.STORE_KEY: self.store}
        request.match_info = {"session_id": self.sid}
        with patch.object(auth, "resolve_client", return_value=("alice", "bearer")), self.assertRaises(asyncio.CancelledError):
            await session_api.dispatch(request, "get")

    async def test_disable_after_reload(self):
        state.CFG.session_store = SessionConfig()
        self.assertEqual((await self.request()).status, 404)
        self.store.get.assert_not_awaited()

    async def test_lifecycle_closes_pool(self):
        await self.client.close()
        self.store.close.assert_awaited_once()


class OffTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_never_opens_pool(self):
        previous = state.CFG
        state.CFG = configuration(False)
        try:
            with patch.object(session_api.PostgresSessionStore, "open") as opening:
                api, _ = app.build_apps()
                async with TestClient(TestServer(api)) as client:
                    self.assertEqual((await client.post(URL, json={"project": "work"})).status, 404)
                opening.assert_not_called()
        finally:
            state.CFG = previous

    async def test_enabled_startup_requires_dsn(self):
        previous = state.CFG
        state.CFG = configuration()
        try:
            with patch.dict(os.environ, {}, clear=True), patch.object(session_api.PostgresSessionStore, "open") as opening:
                api, _ = app.build_apps()
                client = TestClient(TestServer(api))
                try:
                    with self.assertRaisesRegex(RuntimeError, "environment variable"):
                        await client.start_server()
                    opening.assert_not_called()
                finally:
                    await client.close()
        finally:
            state.CFG = previous

    async def test_startup_failure_redacted_and_off_requires_restart(self):
        previous = state.CFG
        state.CFG = configuration()
        try:
            with patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": "postgresql://secret-value"}), \
                    patch.object(session_api.PostgresSessionStore, "open", return_value=UNAVAILABLE):
                api, _ = app.build_apps()
                client = TestClient(TestServer(api))
                try:
                    with self.assertRaisesRegex(RuntimeError, "startup failed") as ctx:
                        await client.start_server()
                    self.assertNotIn("secret-value", str(ctx.exception))
                finally:
                    await client.close()
            state.CFG.session_store = SessionConfig()
            api, _ = app.build_apps()
            async with TestClient(TestServer(api)) as client:
                state.CFG.session_store = SessionConfig(enabled=True)
                response = await client.post(URL, json={"project": "work"}, headers={"Authorization": "Bearer alice"})
                self.assertEqual(response.status, 404)
        finally:
            state.CFG = previous

    async def test_unsupported_url_scheme_refuses_startup_without_opening(self):
        # ADR-0114: the scheme picks the engine; anything else stops the start before a driver or file is touched.
        previous = state.CFG
        state.CFG = configuration()
        try:
            url = "mysql://" + "user:secret-value" + "@192.0.2.70/sessions"
            with patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": url}), \
                    patch.object(session_api.PostgresSessionStore, "open") as opening:
                api, _ = app.build_apps()
                client = TestClient(TestServer(api))
                try:
                    with self.assertRaisesRegex(RuntimeError, "URL scheme not supported") as ctx:
                        await client.start_server()
                    self.assertNotIn("secret-value", str(ctx.exception))
                    opening.assert_not_called()
                finally:
                    await client.close()
        finally:
            state.CFG = previous


class StoreUrlTests(unittest.TestCase):
    """ADR-0114: which engine a connection URL names, and where a SQLite file lies."""
    def test_postgresql_schemes_keep_the_url_for_asyncpg(self):
        asked = []
        for url in ("postgresql://db.example.invalid/skirnir", "postgres://127.0.0.1/skirnir", "PostgreSQL://127.0.0.1/x"):
            self.assertEqual(parse_store_url(url, lambda: asked.append(1)), StoreURL("postgresql"))
        self.assertEqual(asked, [], "the state folder is only needed for a relative SQLite file")

    def test_sqlite_relative_file_lies_in_the_state_folder(self):
        folder = os.path.abspath("synthetic-state")
        self.assertEqual(parse_store_url("sqlite:///sessions.db", folder), StoreURL("sqlite", os.path.join(folder, "sessions.db")))
        self.assertEqual(parse_store_url("sqlite:///store/sessions.db", lambda: folder).path,
                         os.path.join(folder, "store", "sessions.db"))
        absolute = os.path.abspath(os.path.join("elsewhere", "sessions.db"))
        self.assertEqual(parse_store_url("sqlite:///" + absolute, folder).path, absolute)

    def test_unsupported_and_unsafe_urls_are_problems_without_the_url(self):
        for url in ("", None, "test-only", "mysql://" + "u:secret-value" + "@192.0.2.1/x", "sqlite://sessions.db", "sqlite:///",
                    "sqlite:///dir/", "sqlite:///../x.db", "sqlite:///a/../../x.db", "sqlite:///x.db?mode=ro", "sqlite:///x.db#f",
                    "sqlite:///:memory:", "sqlite:///x\x01.db"):
            result = parse_store_url(url, "synthetic-state")
            self.assertTrue(failed(result), url)
            self.assertEqual(result.status, 400)
            self.assertNotIn("secret-value", result.message)


class SqliteTranslationTests(unittest.TestCase):
    """ADR-0122: the SQLite store runs the PostgreSQL statements through a fixed rule set."""
    def test_rules(self):
        cases = {
            "SELECT * FROM skirnir_sessions.sessions WHERE owner=$1 AND id=$2 FOR UPDATE":
                "SELECT * FROM sessions WHERE owner=?1 AND id=?2",
            "UPDATE skirnir_sessions.turns SET lease_until=clock_timestamp()+$4*interval '1 second' WHERE lease=$3":
                "UPDATE turns SET lease_until=clock_timestamp()+CAST(?4*1000000 AS INTEGER) WHERE lease=?3",
            "SELECT 1 WHERE updated_at<clock_timestamp()-$1*interval '1 day' AND x<now()-interval '20 days' ORDER BY a FOR UPDATE SKIP LOCKED LIMIT 1":
                "SELECT 1 WHERE updated_at<clock_timestamp()-CAST(?1*86400000000 AS INTEGER) AND x<now()-1728000000000 ORDER BY a LIMIT 1",
            "SELECT 1 WHERE ('*'=ANY($2::text[]) OR project=ANY($2::text[])) AND session_id = ANY($3::uuid[])":
                "SELECT 1 WHERE ('*' IN (SELECT value FROM json_each(?2)) OR project IN (SELECT value FROM json_each(?2))) "
                "AND session_id IN (SELECT value FROM json_each(?3))",
            "DELETE FROM skirnir_sessions.sessions WHERE (owner,id) IN (SELECT * FROM unnest($1::text[],$2::uuid[]))":
                "DELETE FROM sessions WHERE (owner,id) IN (SELECT a.value,b.value FROM json_each(?1) a JOIN json_each(?2) b ON a.key=b.key)",
            "SELECT operation_id, scope->>'__bank' AS bank, strpos(lower(payload::text),lower($4))>0":
                "SELECT operation_id, json_extract(scope,'$.__bank') AS bank, instr(lower(payload),lower(?4))>0",
            "UPDATE t SET scope=jsonb_build_object('__bank',scope->>'__bank'), retry_ids=retry_ids || jsonb_build_array($5::text)":
                "UPDATE t SET scope=json_object('__bank',json_extract(scope,'$.__bank')), retry_ids=json_insert(retry_ids,'$[#]',?5)",
            "SELECT EXTRACT(EPOCH FROM clock_timestamp()-created_at) FROM x":
                "SELECT ((clock_timestamp()-created_at)/1000000.0) FROM x",
            "INSERT INTO skirnir_sessions.sessions (owner,id) VALUES ($1,$2) RETURNING *":
                "INSERT INTO sessions(created_at,updated_at,owner,id) VALUES(now(),now(),?1,?2) RETURNING *",
            "INSERT INTO skirnir_sessions.handoff_imports(owner,event_id) VALUES($1,$2)":
                "INSERT INTO handoff_imports(owner,event_id) VALUES(?1,?2)",
        }
        for postgres, expected in cases.items():
            self.assertEqual(translate(postgres), expected, postgres)
        for lock in ("SELECT pg_advisory_xact_lock(7365471202492)", "SELECT pg_advisory_xact_lock(hashtextextended($1,0))"):
            self.assertIsNone(translate(lock))

    def test_values_cross_like_asyncpg_hands_them_over(self):
        sid = uuid4()
        self.assertEqual(sqlite_store.parameter(sid), str(sid))
        self.assertEqual(sqlite_store.parameter([sid, "x"]), f'["{sid}", "x"]')
        self.assertEqual(sqlite_store.parameter({"text": "Grüße"}), '{"text": "Grüße"}')
        moment = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        self.assertEqual(sqlite_store.column("created_at", sqlite_store.parameter(moment)), moment)
        self.assertEqual(sqlite_store.column("id", str(sid)), sid)
        self.assertIs(sqlite_store.column("delete_requested", 1), True)
        self.assertEqual(sqlite_store.column("payload", '{"a": [1]}'), {"a": [1]})

    def test_both_engines_provide_the_whole_protocol(self):
        names = [n for n, v in vars(SessionStore).items() if inspect.iscoroutinefunction(v)]
        names += [n for n, v in vars(OutboxStore).items() if inspect.iscoroutinefunction(v)]
        for engine in (PostgresSessionStore, SqliteSessionStore):
            self.assertEqual([n for n in names if not inspect.iscoroutinefunction(getattr(engine, n, None))], [], engine)


if __name__ == "__main__":
    unittest.main(verbosity=2)
