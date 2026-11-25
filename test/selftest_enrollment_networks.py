#!/usr/bin/env python3
"""Self-test (TM-07, ADR-0136): new agent identities enroll only from configured networks.

router.agent_enrollment_networks (CIDR list; empty or absent = any address, as before): a key the registry does not
know yet is refused before it is registered when its address is outside every network; an IPv4-mapped IPv6 address
counts as its IPv4 address. Known identities (approved, pending, revoked) reconnect from anywhere, as before. The
configuration refuses what is no list of networks. Synthetic documentation addresses only; no network."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, registry, state, values  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")


class NetworkValueTests(unittest.TestCase):
    def test_networks_parse_and_match(self):
        nets = [values.ip_network(x, "test") for x in ("192.0.2.0/24", "2001:db8::/32", "198.51.100.7")]
        self.assertFalse(any(failed(n) for n in nets))
        self.assertEqual(str(nets[2]), "198.51.100.7/32", "a single address is a host network")
        self.assertEqual(str(values.ip_network("192.0.2.77/24", "test")), "192.0.2.0/24", "host bits may be set")
        for inside in ("192.0.2.10", "2001:db8::5", "198.51.100.7", "::ffff:192.0.2.10"):
            with self.subTest(inside=inside):
                self.assertTrue(values.in_networks(inside, nets))
        for outside in ("203.0.113.5", "198.51.100.8", "2001:db9::1", "::ffff:203.0.113.5", None, "", "not-an-address"):
            with self.subTest(outside=outside):
                self.assertFalse(values.in_networks(outside, nets))
        for bad in ("nope", "300.1.1.0/24", "192.0.2.0/33", ""):
            with self.subTest(bad=bad):
                self.assertTrue(failed(values.ip_network(bad, "test")))


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.previous = state.CFG, state.REG
        registry._ENROLL.clear()
        state.REG = SimpleNamespace(nodes={"approved-key": {"state": "approved"}, "pending-key": {"state": "pending"}})

    def tearDown(self):
        state.CFG, state.REG = self.previous
        registry._ENROLL.clear()

    def configure(self, *networks):
        state.CFG = SimpleNamespace(agent_enrollment_networks=[values.ip_network(n, "test") for n in networks])

    def test_without_networks_any_address_enrolls(self):
        self.configure()
        self.assertIsNone(registry.enrollment_denial("new-key", "203.0.113.9", now=1))

    def test_a_new_key_outside_the_networks_is_refused(self):
        self.configure("192.0.2.0/24", "2001:db8::/32")
        refusal = registry.enrollment_denial("new-key", "203.0.113.9", now=1)
        self.assertEqual(refusal, "enrollment from 203.0.113.9 is outside router.agent_enrollment_networks")
        self.assertEqual(registry._ENROLL, {}, "a refused key uses no enrollment slot")
        self.assertIsNotNone(registry.enrollment_denial("new-key", None, now=1), "an unknown address is outside")
        self.assertIsNone(registry.enrollment_denial("new-key", "192.0.2.9", now=1), "inside: enrolls")
        self.assertIsNone(registry.enrollment_denial("other-key", "2001:db8::9", now=1), "inside (IPv6): enrolls")

    def test_known_identities_reconnect_from_anywhere(self):
        self.configure("192.0.2.0/24")
        for fp in ("approved-key", "pending-key"):
            with self.subTest(fp=fp):
                self.assertIsNone(registry.enrollment_denial(fp, "203.0.113.9", now=1))


class ConfigurationTests(unittest.TestCase):
    def load(self, value):
        with open(EXAMPLE, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if value is not None:
            raw["router"]["agent_enrollment_networks"] = value
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f)
            return config.Config.load(path, use_overrides=False)

    def test_configuration(self):
        cfg = self.load(None)
        self.assertEqual(cfg.agent_enrollment_networks, [], "absent: any address, as before")
        cfg = self.load(["192.0.2.0/24", "2001:db8::/32"])
        self.assertEqual([str(n) for n in cfg.agent_enrollment_networks], ["192.0.2.0/24", "2001:db8::/32"])
        for bad in ("192.0.2.0/24", ["not-a-network"], [5], ["192.0.2.0/24", "300.0.0.0/8"]):
            with self.subTest(bad=bad):
                problem = self.load(bad)
                self.assertTrue(failed(problem), f"{bad!r} is refused")
                self.assertIn("agent_enrollment_networks", problem.message)


if __name__ == "__main__":
    unittest.main(verbosity=1)
