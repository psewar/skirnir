#!/usr/bin/env python3
"""Self-test (TM-03, ADR-0143): the admin part of the control port answers only router.admin_networks.

router.admin_networks (CIDR list; empty or absent = any address, as before): the web UI, its files, /admin/* and
/metrics refuse an address outside every network with 403 before any login check, so it gets no password attempt and
uses no failure slot, also with valid credentials and with the unauthenticated loopback switch; an IPv4-mapped IPv6
address counts as its IPv4 address. The agents' paths (/v1/...) are not affected, and in the real control app no /v1/
path (dot segments, encoded slashes) resolves to an admin handler. The configuration refuses what is no list of
networks and a list that would lock out a loopback listener, and the web UI cannot change it. The posture report names
an admin part that answers every network. Synthetic documentation addresses only; no network."""
import asyncio
import base64
import copy
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import yaml
from aiohttp.test_utils import make_mocked_request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, auth, config, posture, settings, state, values  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
PASSWORD = "synthetic-admin-password"
AGENT_PATHS = {"/v1/tunnel", "/v1/agent/binary/{name}", "/v1/heartbeat/{node}"}


class Request:
    def __init__(self, path, remote, *, method="GET", headers=None):
        self.path, self.remote, self.method, self.headers = path, remote, method, headers or {}


def call(request):
    handler = AsyncMock(return_value="allowed")
    result = asyncio.run(auth.basic_auth_middleware(request, handler))
    return result, handler.await_count


def status(result):
    return result if result == "allowed" else result.status


class MiddlewareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stored = auth.hash_password(PASSWORD)
        cls.login = {"Authorization": "Basic " + base64.b64encode(f"operator:{PASSWORD}".encode()).decode()}

    def setUp(self):
        self.previous = state.CFG
        for table in (auth._BASIC_OK, auth._BASIC_FAIL, auth._BASIC_SRC):
            table.clear()

    def tearDown(self):
        state.CFG = self.previous
        for table in (auth._BASIC_OK, auth._BASIC_FAIL, auth._BASIC_SRC):
            table.clear()

    def configure(self, *networks, listen="0.0.0.0:11435", users=True, open_loopback=False):
        state.CFG = SimpleNamespace(control_users={"operator": self.stored} if users else {}, control_listen=listen,
                                    control_open_loopback=open_loopback,
                                    admin_networks=[values.ip_network(n, "test") for n in networks])

    def test_without_networks_any_address_reaches_the_login(self):
        self.configure()
        self.assertEqual(status(call(Request("/admin/state", "203.0.113.9"))[0]), 401, "as before: the login decides")
        self.assertEqual(call(Request("/admin/state", "203.0.113.9", headers=self.login))[0], "allowed")

    def test_an_address_outside_is_refused_before_the_login(self):
        self.configure("192.0.2.0/24", "2001:db8::/32")
        for path in ("/", "/ui.js", "/ui.css", "/favicon.ico", "/admin/state", "/admin/config", "/metrics", "/admin/ha"):
            for remote in ("203.0.113.9", "::ffff:203.0.113.9", "2001:db9::1", None, "not-an-address"):
                with self.subTest(path=path, remote=remote):
                    result, reached = call(Request(path, remote, headers=self.login))
                    self.assertEqual((status(result), reached), (403, 0), "refused even with valid credentials")
                    self.assertIn("router.admin_networks", result.text)
        result, _ = call(Request("/admin/config", "203.0.113.9", method="PUT", headers={**self.login, "X-Skirnir-UI": "1"}))
        self.assertEqual(status(result), 403, "a write from outside is refused as well")
        self.assertEqual((auth._BASIC_SRC, auth._BASIC_FAIL, auth._BASIC_OK), ({}, {}, {}),
                         "no password was checked and no failure slot was used")

    def test_an_address_inside_goes_on_to_the_login(self):
        self.configure("192.0.2.0/24", "2001:db8::/32")
        for remote in ("192.0.2.9", "::ffff:192.0.2.9", "2001:db8::5"):
            with self.subTest(remote=remote):
                self.assertEqual(status(call(Request("/admin/state", remote))[0]), 401, "inside: the login still decides")
                self.assertEqual(call(Request("/admin/state", remote, headers=self.login))[0], "allowed")

    def test_the_development_switch_does_not_open_it(self):
        self.configure("192.0.2.0/24", listen="127.0.0.1:11435", users=False, open_loopback=True)
        result, reached = call(Request("/admin/state", "127.0.0.1"))
        self.assertEqual((status(result), reached), (403, 0))

    def test_the_agents_paths_are_not_affected(self):
        self.configure("192.0.2.0/24")
        for path in ("/v1/tunnel", "/v1/agent/binary/skirnir-agent.exe", "/v1/heartbeat/synthetic-node"):
            with self.subTest(path=path):
                self.assertEqual(call(Request(path, "203.0.113.9")), ("allowed", 1))


class ControlAppTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG
        state.CFG = config.Config(EXAMPLE, use_overrides=False)   # build_apps reads which APIs are on

    def tearDown(self):
        state.CFG = self.previous

    def test_every_control_route_outside_v1_is_behind_the_networks(self):
        async def resolve():
            _, ctl = app.build_apps()
            ctl.router.freeze()
            paths = {r.resource.canonical for r in ctl.router.routes()}
            found = {}
            for path in ("/v1/../admin/state", "/v1/%2e%2e/admin/state", "/v1%2F..%2Fadmin/state", "/v1/..%2Fadmin/config"):
                info = await ctl.router.resolve(make_mocked_request("GET", path, app=ctl))
                found[path] = info.http_exception.status if info.http_exception else info.handler
            return paths, found

        paths, found = asyncio.run(resolve())
        self.assertEqual({p for p in paths if p.startswith("/v1/")}, AGENT_PATHS,
                         "a new /v1/ route on the control port bypasses the admin networks: decide it here")
        self.assertTrue({"/", "/ui.js", "/admin/state", "/admin/config", "/metrics"} <= paths)
        for path, outcome in found.items():
            with self.subTest(path=path):
                self.assertEqual(outcome, 404, "a /v1/ path never resolves to an admin handler")


class ConfigurationTests(unittest.TestCase):
    def load(self, value, listen="0.0.0.0:11435"):
        with open(EXAMPLE, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw["router"]["control_listen"] = listen
        if value is not None:
            raw["router"]["admin_networks"] = value
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f)
            return config.Config.load(path, use_overrides=False)

    def test_configuration(self):
        self.assertEqual(self.load(None).admin_networks, [], "absent: any address, as before")
        cfg = self.load(["192.0.2.0/24", "2001:db8::/32"])
        self.assertEqual([str(n) for n in cfg.admin_networks], ["192.0.2.0/24", "2001:db8::/32"])
        for bad in ("192.0.2.0/24", ["not-a-network"], [5], ["192.0.2.0/24", "300.0.0.0/8"]):
            with self.subTest(bad=bad):
                problem = self.load(bad)
                self.assertTrue(failed(problem), f"{bad!r} is refused")
                self.assertIn("admin_networks", problem.message)

    def test_the_web_ui_cannot_change_the_list(self):
        # a stolen admin login must not widen it: PUT /admin/config goes through settings.apply (only EDITABLE paths)
        for value in (["0.0.0.0/0"], [], None):
            with self.subTest(value=value):
                problem = settings.apply({"router": {}}, {"router.admin_networks": value})
                self.assertTrue(failed(problem))
                self.assertIn("cannot be changed in the UI", problem.message)

    def test_a_loopback_listener_needs_a_loopback_network(self):
        for listen in ("127.0.0.1:11435", "[::1]:11435", "localhost:11435"):
            with self.subTest(listen=listen):
                problem = self.load(["192.0.2.0/24"], listen)
                self.assertTrue(failed(problem), "every admin request would be refused")
                self.assertIn("lists no loopback address", problem.message)
        self.assertFalse(failed(self.load(["127.0.0.1/32"], "127.0.0.1:11435")))
        self.assertFalse(failed(self.load(["::1/128"], "[::1]:11435")))
        self.assertFalse(failed(self.load(["192.0.2.0/24"], "0.0.0.0:11435")), "a network listener needs no loopback")


class PostureTests(unittest.TestCase):
    def codes(self, listen, networks):
        cfg = config.Config.__new__(config.Config)
        raw = copy.deepcopy(config.load_yaml(EXAMPLE))
        raw["router"]["control_listen"] = listen
        raw["router"]["admin_networks"] = networks
        self.assertFalse(failed(cfg.parse(raw)))
        return {f.code: f for f in posture.findings(cfg, {})}

    def test_posture_names_an_admin_part_open_to_every_network(self):
        found = self.codes("0.0.0.0:11435", [])
        self.assertEqual(found["admin_from_any_network"].severity, "info", "a hardening fact; --init stays without medium findings")
        self.assertEqual(found["admin_from_any_network"].risk, "TM-03")
        self.assertIn("router.admin_networks", found["admin_from_any_network"].next_step)
        self.assertNotIn("admin_from_any_network", self.codes("0.0.0.0:11435", ["192.0.2.0/24"]))
        self.assertNotIn("admin_from_any_network", self.codes("127.0.0.1:11435", []), "loopback only: nothing to segment")


if __name__ == "__main__":
    unittest.main(verbosity=1)
