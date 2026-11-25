#!/usr/bin/env python3
"""Self-test (TM-03, ADR-0065, ADR-0128): browser-facing protections of the control UI.

Headers: the UI, its script, admin JSON and refusals carry the Content-Security-Policy (scripts only from the router: no
'unsafe-inline', no 'unsafe-eval'; no foreign code, connect-src 'self', frame-ancestors 'none', ...), X-Frame-Options
DENY, nosniff and no referrer; the API port carries nosniff and no referrer. /ui.js needs the control login like the page.
CORS: no answer carries Access-Control-Allow-*, also not for a foreign Origin or a preflight.
UI (static, ui.html and ui.js): no inline event handler and no inline script anywhere, also not in markup the script
builds; every data-args value goes through args() (esc() of a JSON array; esc() escapes the single quote); every action an
element names exists in ACTIONS; nothing is evaluated as code; the UI loads nothing from another origin.
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
UI_JS = os.path.join(HERE, "..", "router", "ui.js")
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
        for path in ("/", "/ui.js", "/admin/state"):
            r = await self.ctl.get(path, headers=auth_header)
            await r.read()
            self.assertEqual(r.status, 200, path)
            self.assert_control_headers(r, path)
        for path in ("/admin/state", "/ui.js"):   # rejected: no login, no script either
            r = await self.ctl.get(path, headers={"Origin": FOREIGN})
            self.assertEqual(r.status, 401, path)
            self.assert_control_headers(r, f"refusal {path}")
        r = await self.ctl.options("/admin/state", headers={"Origin": FOREIGN, "Access-Control-Request-Method": "POST"})
        self.assert_control_headers(r, "preflight")

    async def test_script_is_served_as_a_file_without_cache(self):
        r = await self.ctl.get("/ui.js", headers={"Authorization": basic("operator", PASSWORD)})
        body = await r.text()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers.get("Content-Type", "").startswith("text/javascript"), r.headers.get("Content-Type"))
        self.assertEqual(r.headers.get("Cache-Control"), "no-store", "page and script always from the same installation")
        self.assertIn("const ACTIONS = {", body)

    def test_policy_runs_only_the_routers_own_scripts(self):
        directives = dict(d.strip().split(" ", 1) for d in browser_headers.CONTROL_CSP.split(";"))
        self.assertEqual(directives["script-src"], "'self'", "no 'unsafe-inline', no 'unsafe-eval', no other source")
        self.assertEqual(directives["default-src"], "'self'")
        self.assertNotIn("'unsafe-eval'", browser_headers.CONTROL_CSP)

    async def test_api_port_headers(self):
        r = await self.api.post("/api/chat", json={"model": "x", "messages": []}, headers={"Origin": FOREIGN})
        await r.read()
        self.assertEqual(r.status, 401)
        self.assert_common(r, "api refusal")
        self.assertNotIn("Content-Security-Policy", r.headers, "the API port serves no UI")


# onclick=, onerror="...", oninput = '...' as an attribute; not a variable such as `const onNodes = ...`
INLINE_HANDLER = re.compile(r"\son[a-z]+=|\son[a-z]+\s*=\s*[\"']")


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.ui = f.read()
        with open(UI_JS, encoding="utf-8") as f:
            cls.js = f.read()
        body = re.search(r"\nconst ACTIONS = \{\n(.*?)\n\};\n", cls.js, re.S)
        cls.actions = set(re.findall(r"^  (\w+): ", body.group(1), re.M)) if body else set()

    def test_esc_and_args_helpers(self):
        esc = re.search(r"const esc = s => (.*);", self.js)
        self.assertIsNotNone(esc)
        self.assertIn("'", esc.group(1).split(".replace(", 1)[1].split(",")[0], "esc() matches the single quote")
        self.assertIn("&#39;", esc.group(1))
        self.assertIn("const args = (...xs) => esc(JSON.stringify(xs));", self.js)

    def test_no_inline_script_and_no_inline_handler(self):
        self.assertEqual(INLINE_HANDLER.findall(self.ui), [], "ui.html: no inline event handler")
        self.assertEqual(re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", self.ui), [], "ui.html: no inline script")
        self.assertEqual(re.findall(r'<script src="([^"]+)"', self.ui), ["ui.js"])
        self.assertEqual(INLINE_HANDLER.findall(self.js), [], "ui.js builds no markup with an inline handler")
        self.assertNotIn("<script", self.js, "ui.js builds no script element")
        for code in ("eval(", "new Function", "javascript:", "setTimeout('", 'setTimeout("', "setTimeout(`"):
            self.assertNotIn(code, self.js, f"nothing is evaluated as code: {code}")

    def test_data_args_go_through_args(self):
        values = re.findall(r'data-args="([^"]*)"', self.js)
        self.assertGreater(len(values), 40, "instrument: action arguments found")
        bad = [v for v in values if not re.fullmatch(r"\$\{args\([^`]*\)\}", v)]
        self.assertEqual(bad, [], "every data-args value is ${args(...)}, i.e. an escaped JSON array")
        self.assertEqual(re.findall(r'data-args="', self.ui), [], "static markup passes one string with data-arg")

    def test_every_named_action_exists(self):
        self.assertGreater(len(self.actions), 30, "instrument: ACTIONS parsed")
        named = set(re.findall(r'data-(?:click|change|input)="(\w+)"', self.ui + self.js))
        self.assertGreater(len(named), 30, "instrument: actions named in the markup")
        self.assertEqual(sorted(named - self.actions), [], "an element names an action that does not exist")
        self.assertEqual(sorted(self.actions - named), [], "ACTIONS holds an action no element names")

    def test_one_capturing_listener_per_event_type(self):
        self.assertIn("for (const type of ['click', 'change', 'input'])", self.js)
        self.assertIn("ev.composedPath()", self.js)
        self.assertIn("if (!Object.hasOwn(ACTIONS, name))", self.js, "only listed actions, nothing inherited")
        self.assertRegex(self.js, r"\}, true\);\n\}", "the listener runs in the capture phase")

    def test_ui_loads_nothing_from_elsewhere(self):
        for name, text in (("ui.html", self.ui), ("ui.js", self.js)):
            self.assertEqual(re.findall(r'(?:src|href)="https?://', text), [], f"{name}: only the router's own origin")
        self.assertNotIn("connect-src 'self' https:", browser_headers.CONTROL_CSP)


if __name__ == "__main__":
    unittest.main(verbosity=1)
