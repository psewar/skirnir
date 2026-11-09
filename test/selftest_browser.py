#!/usr/bin/env python3
"""Self-test (TM-03, ADR-0065): browser-facing protections of the control UI.

Headers: the UI, admin JSON and refusals carry the Content-Security-Policy (no foreign code, connect-src 'self',
frame-ancestors 'none', ...), X-Frame-Options DENY, nosniff and no referrer; the API port carries nosniff and no referrer.
CORS: no answer carries Access-Control-Allow-*, also not for a foreign Origin or a preflight.
UI escaping (static, ui.html): esc() escapes the single quote, js() makes a JSON string literal and HTML-escapes it, every
interpolation inside an inline event handler goes through js() (or is a loop index / a fixed property kind), no handler
puts an interpolation between quotes, and the UI loads nothing from another origin (which the CSP would block).
Synthetic values only."""
import base64
import os
import re
import sys
import unittest

from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, auth, browser_headers, config, state  # noqa: E402

UI = os.path.join(HERE, "..", "router", "ui.html")
PASSWORD = "synthetic-operator-password"
FOREIGN = "https://foreign.example.invalid"


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class HeaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = state.CFG, state.CLIENT_AUTH_MODE
        cfg = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
        cfg.control_users = {"operator": auth.hash_password(PASSWORD, iterations=1000)}
        cfg.client_auth = {"mode": "enforce", "audit_log": None, "clients": {}}
        state.CFG, state.CLIENT_AUTH_MODE = cfg, None
        api, ctl = app.build_apps()
        self.api, self.ctl = TestClient(TestServer(api)), TestClient(TestServer(ctl))
        await self.api.start_server()
        await self.ctl.start_server()

    async def asyncTearDown(self):
        await self.api.close()
        await self.ctl.close()
        state.CFG, state.CLIENT_AUTH_MODE = self.previous

    def assert_control_headers(self, r, where):
        csp = r.headers.get("Content-Security-Policy", "")
        for part in ("frame-ancestors 'none'", "connect-src 'self'", "object-src 'none'", "base-uri 'none'", "default-src 'self'"):
            self.assertIn(part, csp, where)
        self.assertEqual(r.headers.get("X-Frame-Options"), "DENY", where)
        self.assert_common(r, where)

    def assert_common(self, r, where):
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff", where)
        self.assertEqual(r.headers.get("Referrer-Policy"), "no-referrer", where)
        self.assertFalse([h for h in r.headers if h.lower().startswith("access-control-")], f"{where}: no CORS header")

    async def test_control_port_headers(self):
        auth_header = {"Authorization": basic("operator", PASSWORD), "Origin": FOREIGN}
        for path in ("/", "/admin/state"):
            r = await self.ctl.get(path, headers=auth_header)
            await r.read()
            self.assertEqual(r.status, 200, path)
            self.assert_control_headers(r, path)
        r = await self.ctl.get("/admin/state", headers={"Origin": FOREIGN})
        self.assertEqual(r.status, 401)
        self.assert_control_headers(r, "refusal")
        r = await self.ctl.options("/admin/state", headers={"Origin": FOREIGN, "Access-Control-Request-Method": "POST"})
        self.assert_control_headers(r, "preflight")

    async def test_api_port_headers(self):
        r = await self.api.post("/api/chat", json={"model": "x", "messages": []}, headers={"Origin": FOREIGN})
        await r.read()
        self.assertEqual(r.status, 401)
        self.assert_common(r, "api refusal")
        self.assertNotIn("Content-Security-Policy", r.headers, "the API port serves no UI")


class UiEscapingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.ui = f.read()

    def test_esc_and_js_helpers(self):
        esc = re.search(r"const esc = s => (.*);", self.ui)
        self.assertIsNotNone(esc)
        self.assertIn("'", esc.group(1).split(".replace(", 1)[1].split(",")[0], "esc() matches the single quote")
        self.assertIn("&#39;", esc.group(1))
        self.assertRegex(self.ui, r"const js = s => esc\(JSON\.stringify\(String\(s \?\? ''\)\)\);")

    def test_inline_handlers_take_values_only_through_js(self):
        handlers = re.findall(r'\bon(?:click|change|input)="([^"]*)"', self.ui)
        self.assertGreater(len(handlers), 50, "instrument: handlers found")
        bad = []
        for js in handlers:
            if "'${" in js:
                bad.append(("quoted interpolation", js[:90]))
            for expr in re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", js):
                if not (expr.startswith("js(") or expr in ("i", "kind", "x")):
                    bad.append((expr, js[:90]))
        self.assertEqual(bad, [], "every value inside an inline handler goes through js()")

    def test_ui_loads_nothing_from_elsewhere(self):
        self.assertEqual(re.findall(r'(?:src|href)="https?://', self.ui), [], "the CSP allows only the router's own origin")
        self.assertNotIn("connect-src 'self' https:", browser_headers.CONTROL_CSP)


if __name__ == "__main__":
    unittest.main(verbosity=1)
