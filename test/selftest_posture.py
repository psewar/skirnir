#!/usr/bin/env python3
"""Self-test (TM-01, TM-11; ADR-0064): the security posture of a configuration is reported as facts.

Builds synthetic configurations and checks each finding of posture.findings: observe mode, unlocked policy, shared
tokens, address-only clients (info when limited to rerank), control port without users or open on loopback, API port
without TLS, a static node over plain HTTP, and the session store's TLS mode read from the DSN in the environment
(never printed). A configuration without any of these reports POSTURE: ok, and no output contains a token hash or the
DSN. Also runs `router.py --posture` as a process. Synthetic values only."""
import copy
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, posture  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
ROUTER = os.path.join(HERE, "..", "router", "router.py")
DSN_SECRET = "synthetic-db-password-7c1e"


def dsn(host, query=""):
    """A synthetic PostgreSQL DSN with a password, assembled so the publication guard stays quiet."""
    return "postgresql://" + "skirnir:" + DSN_SECRET + "@" + host + ":5432/skirnir" + query


def token(name):
    return hashlib.sha256(name.encode()).hexdigest()


def clean_raw():
    raw = copy.deepcopy(config.load_yaml(EXAMPLE))
    r = raw["router"]
    r["client_auth"] = {"mode": "enforce", "locked": True, "clients": {
        "agent-a": {"token_sha256": token("agent-a")},
        "rerank-only": {"ip": ["192.0.2.40"], "inference": False, "rerank": True}}}
    r["control_auth"] = {"users": {"operator": "pbkdf2:1000:00:00"}}
    r["api_tls"] = True
    r["control_tls"] = {"cert": "/synthetic/cert.pem", "key": "/synthetic/key.pem"}
    r["control_listen"] = "127.0.0.1:11435"   # a network address adds an information finding
    raw["nodes"] = {}
    return raw


def load(raw):
    cfg = config.Config.__new__(config.Config)
    result = cfg.parse(copy.deepcopy(raw))
    assert not failed(result), getattr(result, "message", result)
    return cfg


def codes(cfg, environ=None):
    """Highest severity per finding code (several clients can share one code)."""
    rank = {"high": 0, "medium": 1, "info": 2}
    out = {}
    for f in posture.findings(cfg, environ or {}):
        if f.code not in out or rank[f.severity] < rank[out[f.code]]:
            out[f.code] = f.severity
    return out


class PostureTests(unittest.TestCase):
    def test_clean_configuration_reports_ok(self):
        found = codes(load(clean_raw()))
        self.assertEqual(found, {"client_ip_only": "info"}, "a rerank-only address client is only information")
        raw = clean_raw()
        del raw["router"]["client_auth"]["clients"]["rerank-only"]
        lines, summary = posture.report(load(raw), {})
        self.assertEqual((lines, summary), ([], "POSTURE: ok"))

    def test_each_finding(self):
        cases = []
        raw = clean_raw()
        raw["router"]["client_auth"]["mode"] = "observe"
        cases.append((raw, "client_auth_observe", "high"))
        raw = clean_raw()
        raw["router"]["client_auth"]["locked"] = False
        cases.append((raw, "client_auth_unlocked", "medium"))
        raw = clean_raw()
        raw["router"]["client_auth"]["clients"]["agent-b"] = {"token_sha256": token("agent-a")}
        cases.append((raw, "clients_share_token", "high"))
        raw = clean_raw()
        raw["router"]["client_auth"]["clients"]["by-address"] = {"ip": ["192.0.2.41"]}
        cases.append((raw, "client_ip_only", "medium"))
        raw = clean_raw()
        raw["router"]["control_auth"] = {"users": {}}
        cases.append((raw, "control_without_users", "high"))
        raw = clean_raw()
        raw["router"]["control_auth"]["allow_unauthenticated_loopback"] = True
        cases.append((raw, "control_open_loopback", "medium"))
        raw = clean_raw()
        raw["router"]["api_tls"] = False
        cases.append((raw, "api_without_tls", "medium"))
        raw = clean_raw()
        raw["nodes"] = {"plain-node": {"ollama": "http://192.0.2.50:11434"}}
        cases.append((raw, "node_plain_http", "medium"))
        raw = clean_raw()
        raw["router"]["client_auth"]["clients"]["agent-a"]["token_sha256"] = hashlib.sha256(b"changeme").hexdigest()
        cases.append((raw, "client_example_token", "high"))
        raw = clean_raw()
        raw["router"]["control_listen"] = "0.0.0.0:11435"
        cases.append((raw, "control_on_network", "info"))
        raw = clean_raw()
        raw["router"]["control_listen"] = "0.0.0.0:11435"
        del raw["router"]["control_tls"]
        raw["router"]["api_tls"] = False
        cases.append((raw, "control_without_tls", "high"))
        for raw, code, severity in cases:
            with self.subTest(code=code):
                self.assertEqual(codes(load(raw)).get(code), severity)

    def test_loopback_and_https_nodes_are_fine(self):
        raw = clean_raw()
        raw["nodes"] = {"local": {"ollama": "http://127.0.0.1:11434"}, "tls": {"ollama": "https://192.0.2.51:11434"}}
        self.assertNotIn("node_plain_http", codes(load(raw)))

    def test_session_store_tls_from_the_environment(self):
        raw = clean_raw()
        raw["router"]["session_store"] = {"enabled": True}
        cfg = load(raw)
        env = cfg.session_store.dsn_env
        self.assertEqual(codes(cfg, {}).get("session_store_dsn_unset"), "info")
        weak = {env: dsn("192.0.2.60", "?sslmode=require")}
        self.assertEqual(codes(cfg, weak).get("session_store_unverified_tls"), "medium")
        strong = {env: dsn("192.0.2.60", "?sslmode=verify-full&sslrootcert=/x")}
        self.assertNotIn("session_store_unverified_tls", codes(cfg, strong))
        local = {env: dsn("127.0.0.1")}
        self.assertNotIn("session_store_unverified_tls", codes(cfg, local))
        text = "\n".join(posture.report(cfg, weak)[0])
        self.assertNotIn(DSN_SECRET, text, "the DSN is never printed")

    def test_no_token_hash_in_the_report(self):
        raw = clean_raw()
        raw["router"]["client_auth"]["clients"]["agent-b"] = {"token_sha256": token("agent-a")}
        text = "\n".join(posture.report(load(raw), {})[0])
        self.assertNotIn(token("agent-a"), text)
        self.assertNotIn(token("agent-a")[:8], text)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            raw = clean_raw()
            raw["router"]["client_auth"]["mode"] = "observe"
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f, allow_unicode=True)
            run = subprocess.run([sys.executable, ROUTER, "--posture", path, "--pure"], capture_output=True, text=True,
                                 encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
            self.assertEqual(run.returncode, 0, run.stderr[-400:])
            self.assertIn("client_auth_observe", run.stdout)
            self.assertTrue(run.stdout.strip().splitlines()[-1].startswith("POSTURE: 1 high"), run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
