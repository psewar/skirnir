#!/usr/bin/env python3
"""Self-test (TM-09): isolation holds with the Hindsight gateway enabled, across a token rotation and a grant revocation.

selftest_memory.py and selftest_sessions.py enumerate every memory, gateway and session route and check authentication
with the gateway off. This test runs the router with a configuration file in a temporary directory, the gateway on, a
session store (mocked) and a control user, and checks:
  - every gateway route refuses a request without identity (401) also when the gateway is enabled; two clients using the
    same project alias reach different banks;
  - after a token rotation through the admin API the old token is refused on memory, gateway and session routes, the new
    one works, and another client is unaffected;
  - after a grant is revoked in the configuration file and the configuration is reloaded, recall through REST and gateway
    is refused (403) while the client's identity is still valid.
Synthetic values only; the rotation writes roles.yaml into the temporary directory, never into the repository."""
import copy
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import base64

import yaml
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from selftest_memory import BASE, PROJECT, MemoryHarness, policy  # noqa: E402
from skirnir_router import app, auth, config, memory, memory_gateway, session_api, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402
from skirnir_router.session_store import SessionStore  # noqa: E402

PASSWORD = "synthetic-operator-password"
GATEWAY_RECALL = memory_gateway.GATEWAY + "/v1/default/banks/" + PROJECT + "/memories/recall"


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class IsolationRotationTests(MemoryHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.client.close()   # rebuilt below from a configuration file, so rotation and reload work for real
        self.tmp = tempfile.mkdtemp(prefix="skirnir-isolation-")
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        raw = copy.deepcopy(config.load_yaml(BASE))
        raw["router"]["hindsight"] = {"enabled": True, "allow_http_loopback": True, "timeout_s": 0.2,
                                      "retention_enabled": True, "gateway_enabled": True}
        raw["router"]["session_store"] = {"enabled": True}
        raw["router"]["control_auth"] = {"users": {"operator": auth.hash_password(PASSWORD, iterations=1000)}}
        raw["router"]["client_auth"] = {"mode": "enforce", "clients": {name: {
            "token_sha256": hashlib.sha256(name.encode()).hexdigest(), "memory": policy(), "data_class": "personal",
            "models": True, "session_projects": ["work"]} for name in ("client-a", "client-b")}}
        self.raw = raw
        self.write(raw)
        cfg = config.Config.load(self.cfg_path, use_overrides=True)
        self.assertFalse(failed(cfg), getattr(cfg, "message", cfg))
        cfg.roles = {"example": self.role}
        state.CFG, state.CLIENT_AUTH_MODE = cfg, None
        self.store = AsyncMock(spec=SessionStore)
        self.session = {"id": str(uuid4()), "owner": "client-a", "project": "work", "mode": "client-managed", "status": "active"}
        self.store.create.return_value = self.session
        self.store.get.return_value = self.session
        self.session_patches = [patch.object(session_api.PostgresSessionStore, "open", return_value=self.store),
                                patch.dict(os.environ, {"SKIRNIR_SESSION_DSN": "test-only"})]
        for p in self.session_patches:
            p.start()
        api, ctl = app.build_apps()
        self.client, self.ctl = TestClient(TestServer(api)), TestClient(TestServer(ctl))
        await self.client.start_server()
        await self.ctl.start_server()
        self.service = api[memory.SERVICE_KEY]

    async def asyncTearDown(self):
        await self.ctl.close()
        for p in reversed(self.session_patches):
            p.stop()
        await super().asyncTearDown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, raw):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)

    async def as_client(self, token, method, path, body=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        r = await self.client.request(method, path, json=body, headers=headers)
        await r.read()
        return r.status

    async def reach(self, token):
        """Status of REST recall, gateway recall and reading a session for this token."""
        return (await self.as_client(token, "POST", memory.PREFIX + "/recall", {"project": PROJECT, "query": "Synthetic"}),
                await self.as_client(token, "POST", GATEWAY_RECALL, {"query": "Synthetic"}),
                await self.as_client(token, "GET", session_api.PREFIX + "/" + self.session["id"]))

    async def test_gateway_routes_refuse_without_identity_and_keep_banks_apart_when_enabled(self):
        self.assertTrue(state.CFG.hindsight.gateway_enabled, "instrument: the gateway is on")
        routes = set()
        for route in self.client.server.app.router.routes():
            info = route.resource.get_info() if route.resource is not None else {}
            template = info.get("formatter") or info.get("path") or ""
            if template.startswith(memory_gateway.GATEWAY + "/v1/default") and route.method not in ("HEAD", "OPTIONS"):
                routes.add((route.method, template))
        self.assertGreaterEqual(len(routes), 3, sorted(routes))
        for method, template in sorted(routes):
            path = template.replace("{alias}", PROJECT).replace("{operation_id}", "op-synthetic")
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(await self.as_client(None, method, path, {"query": "Synthetic", "items": []}), 401)
                self.assertEqual(await self.as_client("wrong-token", method, path, {"query": "Synthetic", "items": []}), 401)
        self.assertEqual(self.calls, [], "the memory backend is never reached without an identity")
        for token in ("client-a", "client-b"):
            self.assertEqual(await self.as_client(token, "POST", GATEWAY_RECALL, {"query": "Synthetic"}), 200, token)
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0][0], self.calls[1][0], "the same alias maps to a bank per client")

    async def test_token_rotation_refuses_the_old_token_on_memory_gateway_and_session_routes(self):
        self.assertEqual(await self.reach("client-a"), (200, 200, 200))
        r = await self.ctl.post("/admin/clients/client-a/token", headers={"Authorization": basic("operator", PASSWORD), "X-Skirnir-UI": "1"})
        self.assertEqual(r.status, 200)
        new = (await r.json())["token"]
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "roles.yaml")), "instrument: rotation wrote the temporary overrides")
        self.assertEqual(await self.reach("client-a"), (401, 401, 401), "the old token is refused everywhere")
        self.assertEqual(await self.reach(new), (200, 200, 200), "the new token works everywhere")
        self.assertEqual(await self.reach("client-b"), (200, 200, 200), "another client is unaffected")

    async def test_grant_revocation_by_reload_refuses_recall_with_a_valid_identity(self):
        self.assertEqual(await self.reach("client-a"), (200, 200, 200))
        calls = len(self.calls)
        raw = copy.deepcopy(self.raw)
        raw["router"]["client_auth"]["clients"]["client-a"]["memory"]["projects"][PROJECT]["read"] = False
        self.write(raw)
        problem = state.CFG.reload()
        self.assertIsNone(problem, getattr(problem, "message", problem))
        rest, gateway, session = await self.reach("client-a")
        self.assertEqual((rest, gateway), (403, 403), "recall is refused once the read grant is gone")
        self.assertEqual(session, 200, "the identity itself is still valid")
        self.assertEqual(len(self.calls), calls, "the memory backend is not reached after the revocation")
        self.assertEqual(await self.reach("client-b"), (200, 200, 200), "another client is unaffected")


if __name__ == "__main__":
    unittest.main(verbosity=1)
