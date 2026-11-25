#!/usr/bin/env python3
"""Self-test (ADR-0146, ADR-0147): what a model test bench needs from the control API.

POST /admin/measure takes optional `contexts`, so a candidate is measured where a role would run it without a temporary
role; malformed lists are refused before anything is measured, and a node without GPU values (only the /api/ps
measurement at 8k and 32k) is refused instead of silently skipping the contexts. GET /admin/roles answers role ->
model@context in the shape of `roles` in GET /admin/config, without prompts or other configuration, behind the same
admin login. Synthetic configuration only; no network."""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import admin, app, auth, config, perf, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
MODEL = "example-model:8b"


def node(gpu=True, name="example-node"):
    return SimpleNamespace(name=name, gpu_known=lambda now: gpu, vram_used_gib=10.0 if gpu else None)


class MeasureContextsTests(unittest.TestCase):
    def setUp(self):
        self.previous, self.measuring = state.CFG, dict(state.MEASURING)
        state.MEASURING.clear()
        state.CFG = SimpleNamespace(roles={"example": {"tiers": [{"model": MODEL, "num_ctx": 65536}]},
                                           "cloud": {"tiers": [{"model": MODEL, "num_ctx": 999999, "cloud": "example"}]}})

    def tearDown(self):
        state.CFG = self.previous
        state.MEASURING.clear()
        state.MEASURING.update(self.measuring)

    def measure(self, body, target=None):
        """(status, answer, spawned coroutine args) of POST /admin/measure with this body; nothing runs."""
        spawned = []

        def spawn(coroutine):
            spawned.append(coroutine.cr_frame.f_locals.get("extra"))
            coroutine.close()

        with patch.object(perf, "read_json", AsyncMock(return_value=body)), \
                patch.object(perf, "_free_node_for", return_value=target or node()), patch.object(state, "spawn", spawn):
            response = asyncio.run(perf.handle_measure(SimpleNamespace()))
        import json
        return response.status, json.loads(response.body), spawned

    def test_contexts_are_measured_besides_the_fixed_and_the_roles_contexts(self):
        self.assertEqual(perf.measure_ctxs(MODEL), [8192, 32768, 65536])   # unchanged without contexts; cloud tiers do not count
        self.assertEqual(perf.measure_ctxs(MODEL, (196608, 32768)), [8192, 32768, 65536, 196608])
        status, answer, spawned = self.measure({"model": MODEL, "contexts": [196608]})
        self.assertEqual((status, answer["contexts"], answer["real"]), (200, [8192, 32768, 65536, 196608], True))
        self.assertEqual(spawned, [(196608,)])
        status, answer, spawned = self.measure({"model": MODEL})   # as before: no contexts, no change
        self.assertEqual((status, answer["contexts"], spawned), (200, [8192, 32768, 65536], [()]))

    def test_malformed_contexts_are_refused_before_anything_is_measured(self):
        for contexts in ([], [196608] * 2, [1024], [2 ** 21], ["196608"], [196608.0], [True], 196608, list(range(4096, 4103))):
            with self.subTest(contexts=contexts):
                self.assertTrue(failed(perf.measure_extra(contexts)))
                status, answer, spawned = self.measure({"model": MODEL, "contexts": contexts})
                self.assertEqual((status, spawned), (400, []))
                self.assertIn("contexts must be", answer["error"])
        self.assertEqual(perf.measure_extra(None), ())
        self.assertEqual(perf.measure_extra([2048, 1048576]), (2048, 1048576))

    def test_a_node_without_gpu_values_is_refused_for_contexts_not_silently_measured_at_8k_and_32k(self):
        status, answer, spawned = self.measure({"model": MODEL, "contexts": [196608]}, node(gpu=False))
        self.assertEqual((status, spawned), (409, []))
        self.assertIn("reports no GPU values", answer["error"])
        status, answer, spawned = self.measure({"model": MODEL}, node(gpu=False))   # without contexts: the /api/ps path
        self.assertEqual((status, answer["real"], answer["contexts"], spawned), (200, False, [8192, 32768], [()]))


class RolesTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG
        state.CFG = config.Config(EXAMPLE, use_overrides=False)

    def tearDown(self):
        state.CFG = self.previous

    def test_roles_are_the_config_roles_reduced_to_model_at_context(self):
        lean, full = admin.roles_lean(), config.roles_as_config()
        self.assertEqual(set(lean), set(full))
        self.assertTrue(lean)
        for name, role in lean.items():
            with self.subTest(role=name):
                self.assertEqual(set(role), {"exposed_as", "tiers"})   # no system_append, options, canary, shadow
                self.assertEqual(role["exposed_as"], full[name]["exposed_as"])
                self.assertEqual([(t["model"], t["num_ctx"]) for t in role["tiers"]],
                                 [(t["model"], t["num_ctx"]) for t in full[name]["tiers"]])
                for tier, original in zip(role["tiers"], full[name]["tiers"], strict=True):
                    self.assertEqual(tier.get("cloud"), original.get("cloud"))
                    self.assertLessEqual(set(tier), {"model", "num_ctx", "cloud"})

    def test_the_answer_and_the_route_behind_the_admin_login(self):
        response = asyncio.run(admin.handle_roles(SimpleNamespace()))
        import json
        body = json.loads(response.body)
        self.assertEqual(set(body), {"version", "roles"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        _api, ctl = app.build_apps()
        routes = {(r.method, r.resource.canonical): r.handler for r in ctl.router.routes() if r.resource is not None}
        self.assertIs(routes[("GET", "/admin/roles")], admin.handle_roles)
        self.assertNotIn(("PUT", "/admin/roles"), routes)   # read only
        # Like every control path outside /v1/: no login, no answer.
        state.CFG.control_users = {"operator": auth.hash_password("synthetic-admin-password")}
        state.CFG.control_open_loopback, state.CFG.admin_networks = False, []
        request = SimpleNamespace(path="/admin/roles", remote="192.0.2.10", method="GET", headers={})
        handler = AsyncMock(return_value="allowed")
        result = asyncio.run(auth.basic_auth_middleware(request, handler))
        self.assertEqual((getattr(result, "status", result), handler.await_count), (401, 0))


if __name__ == "__main__":
    unittest.main()
