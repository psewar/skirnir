#!/usr/bin/env python3
"""Self-test (TM-04, ADR-0131): model listings show a client only what it may use.

Over the real API app (no network, no node): /api/tags, /v1/models (OpenAI and Anthropic form) and /v1/models/{model}
list the roles a client is granted and concrete model names only with models:true, as the request path decides
(auth.client_allows, ADR-0052). Unauthenticated requests in observe mode see everything, as before; the router's own
UI client keeps concrete names. Listing refuses nothing, so it counts no forbidden request and writes no audit line.
Synthetic values only."""
import hashlib
import os
import sys
import unittest
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, auth, config, state  # noqa: E402

CONCRETE = "synthetic-concrete:7b"


def token(name):
    return f"synthetic-{name}-token"


class ListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = state.CFG, state.CLIENT_AUTH_MODE, dict(state.NODES)
        cfg = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
        self.roles = list(cfg.roles)
        self.assertGreater(len(self.roles), 2, "instrument: the example configuration has several roles")
        self.granted = cfg.roles[self.roles[0]]["name"]
        clients = {
            "role-only": {"roles": [self.granted], "models": False},
            "all-roles": {"roles": ["*"], "models": False},
            "full": {"roles": ["*"], "models": True},
        }
        for name, c in clients.items():
            c["token_sha256"] = hashlib.sha256(token(name).encode()).hexdigest()
        cfg.client_auth = {"mode": "enforce", "audit_log": None, "clients": clients}
        cfg.expose_concrete = True
        state.CFG, state.CLIENT_AUTH_MODE = cfg, None
        # An offline node that reports a concrete model: listed by name, never chosen for a role's details.
        state.NODES.clear()
        state.NODES["synthetic-node"] = SimpleNamespace(state="offline", model_details={
            CONCRETE: {"name": CONCRETE, "model": CONCRETE, "modified_at": "2026-01-01T00:00:00Z", "size": 1,
                       "digest": "synthetic", "details": {}}})
        self.audited = []
        self.real_audit = auth.audit
        auth.audit = lambda event, **fields: self.audited.append(event)
        api, _ctl = app.build_apps()
        self.api = TestClient(TestServer(api))
        await self.api.start_server()

    async def asyncTearDown(self):
        await self.api.close()
        auth.audit = self.real_audit
        state.CFG, state.CLIENT_AUTH_MODE = self.previous[0], self.previous[1]
        state.NODES.clear()
        state.NODES.update(self.previous[2])

    async def names(self, client, path="/api/tags", headers=None):
        h = {**({"Authorization": "Bearer " + token(client)} if client else {}), **(headers or {})}
        r = await self.api.get(path, headers=h)
        self.assertEqual(r.status, 200, await r.text())
        body = await r.json()
        return [m["name"] for m in body["models"]] if "models" in body else [m["id"] for m in body["data"]]

    async def test_a_client_sees_only_what_it_may_use(self):
        self.assertEqual(await self.names("role-only"), [self.roles[0]], "one granted role, no concrete name")
        self.assertEqual(await self.names("all-roles"), self.roles, "every role, still no concrete name (models:false)")
        self.assertEqual(await self.names("full"), self.roles + [CONCRETE], "models:true also lists concrete names")

    async def test_every_listing_path_filters_alike(self):
        oa = await self.names("role-only", "/v1/models")
        anthropic = await self.names("role-only", "/v1/models", {"anthropic-version": "2023-06-01"})
        self.assertEqual(len(oa), 1)
        self.assertEqual(len(anthropic), 1)
        self.assertNotIn(CONCRETE, oa + anthropic)
        r = await self.api.get("/v1/models/" + CONCRETE, headers={"Authorization": "Bearer " + token("role-only")})
        self.assertEqual(r.status, 404, "a concrete model the client may not use is not described either")
        r = await self.api.get("/v1/models/" + CONCRETE, headers={"Authorization": "Bearer " + token("full")})
        self.assertEqual(r.status, 200)

    async def test_unauthenticated_observe_and_the_ui_client_see_everything(self):
        state.CFG.client_auth["mode"] = "observe"
        state.CLIENT_AUTH_MODE = None
        self.assertEqual(await self.names(None), self.roles + [CONCRETE], "observe without identity: unchanged")
        ui = {"client": state.INTERNAL_CLIENT}
        self.assertIsNone(auth.client_grant(ui, None, concrete=True), "the router's own UI client keeps concrete names")

    async def test_listing_refuses_nothing_and_audits_nothing(self):
        before = dict(state.CLIENT_STATS["clients"].get("role-only") or {}).get("forbidden", 0)
        await self.names("role-only")
        after = (state.CLIENT_STATS["clients"].get("role-only") or {}).get("forbidden", 0)
        self.assertEqual((after, self.audited), (before, []), "a filtered listing is not a forbidden request")

    async def test_a_hidden_role_is_still_refused_when_requested(self):
        # The filter only hides; the request path keeps refusing (rejected case).
        r = await self.api.post("/api/chat", json={"model": self.roles[1], "messages": [{"role": "user", "content": "x"}]},
                                headers={"Authorization": "Bearer " + token("role-only")})
        self.assertEqual(r.status, 403)
        self.assertIn("forbidden", self.audited)


if __name__ == "__main__":
    unittest.main(verbosity=1)
