#!/usr/bin/env python3
"""Self-test (TM-04, ADR-0140, supersedes ADR-0052): a client's concrete model names can be an allowlist.

client_auth.clients.<c>.models is true (every concrete name), false or absent (none, roles only) or a list of concrete
model names: only those may be called, listed (/api/tags, /v1/models) and shown loaded (/api/ps). A name without a tag
means :latest on both sides. The configuration, the settings (UI: "*", empty or a comma list) and the UI's client
creation share one parser and refuse anything else. Roles, the router's own UI client and unidentified observe requests
are unchanged. Synthetic values only; no network or node."""
import hashlib
import os
import sys
import unittest
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import admin, app, auth, config, settings, state, values  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

ALLOWED, OTHER = "synthetic-allowed:7b", "synthetic-other:14b"


def token(name):
    return f"synthetic-{name}-token"


class GrantValueTests(unittest.TestCase):
    def test_the_parser(self):
        self.assertIs(values.model_grant(True, "w"), True)
        self.assertIs(values.model_grant(False, "w"), False)
        self.assertEqual(values.model_grant([" a:1 ", "a:1", "hf.co/org/model:Q4_K_M"], "w"), ["a:1", "hf.co/org/model:Q4_K_M"])
        self.assertIs(values.model_grant([], "w"), False, "an empty list grants nothing")
        for bad in ("yes", 1, None, ["bad name"], [""], [5], ["x"] * 201, {"a": 1}):
            with self.subTest(bad=bad):
                problem = values.model_grant(bad, "clients.c.models")
                self.assertTrue(failed(problem))
                self.assertIn("clients.c.models", problem.message)

    def test_matching_normalises_the_tag(self):
        grant = ["llama-synthetic", "qwen-synthetic:7b", "hf.co/org/model"]
        for model, expected in (("llama-synthetic:latest", True), ("llama-synthetic", True), ("qwen-synthetic:7b", True),
                                ("qwen-synthetic:14b", False), ("hf.co/org/model:latest", True), ("other:latest", False)):
            with self.subTest(model=model):
                self.assertIs(values.model_granted(model, grant), expected)
        self.assertTrue(values.model_granted("anything:1", True))
        self.assertFalse(values.model_granted("anything:1", False))
        self.assertFalse(values.model_granted(None, grant))

    def test_settings_and_ui_client_creation_share_the_parser(self):
        path, raw = "router.client_auth.clients.c.models", {"router": {"client_auth": {"clients": {"c": {}}}}}
        self.assertIs(settings.coerce(path, "*", raw), True)
        self.assertIs(settings.coerce(path, "", raw), False)
        self.assertEqual(settings.coerce(path, f"{ALLOWED}, {OTHER}", raw), [ALLOWED, OTHER])
        self.assertTrue(failed(settings.coerce(path, "bad name", raw)))
        self.assertEqual(admin._client_spec("c", {"models": [ALLOWED]})["models"], [ALLOWED])
        self.assertTrue(failed(admin._client_spec("c", {"models": "yes"})), "a string is no longer read as true")

    def test_configuration(self):
        target = SimpleNamespace()
        self.assertIsNone(config.Config._parse_client_auth(target, {"mode": "enforce", "clients": {
            "listed": {"models": [ALLOWED]}, "all": {"models": True}}}))
        self.assertEqual((target.client_auth["clients"]["listed"]["models"], target.client_auth["clients"]["all"]["models"]),
                         ([ALLOWED], True))
        problem = config.Config._parse_client_auth(SimpleNamespace(), {"mode": "enforce", "clients": {"bad": {"models": "yes"}}})
        self.assertTrue(failed(problem))
        self.assertIn("models", problem.message)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG
        state.CFG = SimpleNamespace(client_auth={"mode": "enforce", "clients": {
            "listed": {"roles": ["*"], "models": [ALLOWED]}, "roles-only": {"roles": ["*"], "models": False}}})
        self.audited = []
        self.real_audit = auth.audit
        auth.audit = lambda event, **fields: self.audited.append(event)

    def tearDown(self):
        state.CFG = self.previous
        auth.audit = self.real_audit

    def test_the_request_path(self):
        concrete = lambda m: {"name": m, "exposed": m}   # noqa: E731
        self.assertIsNone(auth.client_allows(_Req("listed"), concrete(ALLOWED), concrete=True), "listed model allowed")
        refused = auth.client_allows(_Req("listed"), concrete(OTHER), concrete=True)
        self.assertEqual(refused, f"client listed: concrete model {OTHER} not in its models allowlist", "rejected case")
        self.assertEqual(self.audited, ["forbidden"], "the refusal is audited as before")
        self.assertIsNotNone(auth.client_allows(_Req("roles-only"), concrete(ALLOWED), concrete=True))
        self.assertIsNone(auth.client_grant(_Req(state.INTERNAL_CLIENT), concrete(OTHER), concrete=True), "the UI keeps all")
        self.assertIsNone(auth.client_grant(_Req(None), concrete(OTHER), concrete=True), "unidentified observe unchanged")


class _Req(dict):
    def __init__(self, client):
        super().__init__({"client": client} if client else {})
        self.path = "/api/chat"


class ListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = state.CFG, state.CLIENT_AUTH_MODE, dict(state.NODES)
        cfg = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
        cfg.client_auth = {"mode": "enforce", "audit_log": None, "clients": {
            "listed": {"roles": ["*"], "models": [ALLOWED.split(":")[0] + ":7b"],
                       "token_sha256": hashlib.sha256(token("listed").encode()).hexdigest()}}}
        cfg.expose_concrete = True
        state.CFG, state.CLIENT_AUTH_MODE = cfg, None
        details = {m: {"name": m, "model": m, "modified_at": "2026-01-01T00:00:00Z", "size": 1, "digest": "d", "details": {}}
                   for m in (ALLOWED, OTHER)}
        state.NODES.clear()
        state.NODES["synthetic-node"] = SimpleNamespace(state="offline", name="synthetic-node", same_blob=lambda a, b: a == b,
                                                        loaded={ALLOWED: 2.0, OTHER: 3.0}, model_details=details)
        api, _ctl = app.build_apps()
        self.api = TestClient(TestServer(api))
        await self.api.start_server()

    async def asyncTearDown(self):
        await self.api.close()
        state.CFG, state.CLIENT_AUTH_MODE = self.previous[0], self.previous[1]
        state.NODES.clear()
        state.NODES.update(self.previous[2])

    async def get(self, path):
        r = await self.api.get(path, headers={"Authorization": "Bearer " + token("listed")})
        self.assertEqual(r.status, 200, await r.text())
        return await r.json()

    async def test_listings_show_only_allowlisted_concrete_names(self):
        names = [m["name"] for m in (await self.get("/api/tags"))["models"]]
        self.assertIn(ALLOWED, names)
        self.assertNotIn(OTHER, names, "a concrete model outside the allowlist is not listed")
        oa = [m["id"] for m in (await self.get("/v1/models"))["data"]]
        self.assertEqual((ALLOWED in oa, OTHER in oa), (True, False))
        loaded = {m["model"]: m.get("node") for m in (await self.get("/api/ps"))["models"]}
        self.assertEqual(loaded.get(ALLOWED), "synthetic-node", "an allowlisted loaded model with its node")
        self.assertNotIn(OTHER, loaded, "a loaded model outside the allowlist is not named")


if __name__ == "__main__":
    unittest.main(verbosity=1)
