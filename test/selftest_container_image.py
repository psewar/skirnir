#!/usr/bin/env python3
"""Self-test (ADR-0116, ADR-0121): the router image's files say what the design promises, without a container engine.

Static checks of router/Dockerfile, router/compose.yaml, router/container/ and router/requirements-container-lock.txt:
  * base image pinned by digest, on a Python the locks cover; packages only from the container lock, every entry
    hash-checked, wheels only, no dependency resolution; the image runs as uid/gid 10001, names no secret, copies no
    configuration; it ships every program file router/install.sh installs on a host (derived from install.sh);
  * the image's folders are the host layout's (router-layout.sh, config.minimal.yaml, the audit log default);
  * the container lock covers the router's requirements on every supported Python and pins the versions CI tests;
  * compose.yaml keeps the unit's limits (no root, no capabilities, no privilege gain, read-only root and configuration),
    takes secrets only from env_file, never pulls the router image, and keeps optional services behind profiles;
  * container/healthcheck.py derives port and scheme from config.yaml like the router and reports a healthy router,
    an error status, a non-router answer, nothing listening and a missing configuration as facts (plain HTTP and TLS
    against local fake servers).
The build and a real start are test/smoke_container.sh (CI job "container"). Synthetic values only.
    python test/selftest_container_image.py
"""
import http.server
import importlib.util
import json
import os
import re
import shlex
import ssl
import sys
import tempfile
import threading
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ROUTER = os.path.join(ROOT, "router")
sys.path.insert(0, os.path.join(ROOT, "tools", "security"))
import lock_requirements  # noqa: E402
import pydeps  # noqa: E402  (lock_requirements put router/skirnir_router on the path)

SECRET_NAME = re.compile(r"PASSWORD|TOKEN|SECRET|DSN|API_KEY|PRIVATE", re.I)
# Files install.sh copies that only a host needs: the systemd-side tools and the host lock.
HOST_ONLY = {"render-env.sh", "router-layout.sh", "render-env.conf.example", "install.sh", "requirements-lock.txt"}


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def dockerfile_instructions():
    """[(INSTRUCTION, argument text)] with continuation lines joined and comments dropped."""
    lines, current = [], ""
    for raw in read("router", "Dockerfile").splitlines():
        if raw.lstrip().startswith("#") and not current:
            continue
        current += raw.rstrip("\\").strip() + " " if raw.rstrip().endswith("\\") else raw.strip()
        if not raw.rstrip().endswith("\\"):
            if current.strip():
                word, _, rest = current.strip().partition(" ")
                lines.append((word.upper(), rest.strip()))
            current = ""
    return lines


def shell_value(text, name):
    m = re.search(rf"^{name}=(\S+)", text, re.M)
    return m.group(1) if m else None


def load_healthcheck():
    spec = importlib.util.spec_from_file_location("healthcheck", os.path.join(ROUTER, "container", "healthcheck.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DockerfileTests(unittest.TestCase):
    def setUp(self):
        self.ins = dockerfile_instructions()

    def args(self, word):
        return [a for w, a in self.ins if w == word]

    def test_base_image_pinned_on_a_locked_python(self):
        froms = self.args("FROM")
        self.assertEqual(len(froms), 1, froms)
        m = re.fullmatch(r"python:(\d+\.\d+)-slim(?:-\w+)?@sha256:[0-9a-f]{64}", froms[0])
        self.assertIsNotNone(m, froms[0])
        self.assertIn(m.group(1), lock_requirements.PYTHONS, "the image's Python must be one the locks are checked for")

    def test_packages_only_from_the_container_lock(self):
        installs = [a for a in self.args("RUN") if "pip install" in a]
        self.assertEqual(len(installs), 1, installs)
        words = shlex.split(installs[0].split("&&")[0])
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-cache-dir"):
            self.assertIn(flag, words)
        self.assertEqual(words[words.index("-r") + 1], "/opt/skirnir-router/requirements-container-lock.txt")
        self.assertIn("pydeps.py verify", installs[0])
        self.assertIn("pip check", installs[0])

    def test_unprivileged_account_and_no_secret(self):
        self.assertEqual(self.args("USER"), ["10001:10001"])
        run = " ".join(self.args("RUN"))
        self.assertIn("--uid 10001", run)
        self.assertIn("--gid 10001", run)
        for word in ("ENV", "ARG"):
            for a in self.args(word):
                for name in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", a) or [a.split("=")[0]]:
                    self.assertIsNone(SECRET_NAME.search(name), f"{word} {name} looks like a secret")
        copies = " ".join(self.args("COPY"))
        for forbidden in ("config.yaml ", "secrets.env", "certs", ".pem"):
            self.assertNotIn(forbidden, copies + " ")

    def test_ships_every_program_file_a_host_gets(self):
        # the loop that copies program files into the install folder: for f in ...; do [ -e "$SRC/$f" ] && cp ...
        m = re.search(r"for f in ([^;]*?); do\s*\[ -e \"\$SRC/\$f\" \] && cp", read("router", "install.sh"), re.S)
        self.assertIsNotNone(m, "install.sh copy loop not found")
        host_files = set(m.group(1).replace("\\", " ").split()) - HOST_ONLY
        copied = set()
        for a in self.args("COPY"):
            copied |= {os.path.basename(w.rstrip("/")) for w in a.split() if not w.startswith("--")}
        self.assertIn("skirnir_router", copied)
        self.assertEqual(host_files - copied, set(), "program files install.sh installs but the image lacks")

    def test_folders_are_the_host_layout(self):
        layout = read("router", "router-layout.sh")
        state_dir, log_dir = shell_value(layout, "STATE_DIR"), shell_value(layout, "LOG_DIR")
        config_dir = shell_value(layout, "CONFIG_DIR")
        self.assertEqual(json.loads(self.args("VOLUME")[0]), [state_dir, log_dir])
        minimal = yaml.safe_load(read("router", "config.minimal.yaml"))
        self.assertEqual(minimal["router"]["state_dir"], state_dir)
        self.assertIn(f'"{log_dir}/audit.jsonl"', read("router", "skirnir_router", "config.py"))
        self.assertEqual(json.loads(self.args("CMD")[0]), [f"{config_dir}/config.yaml"])
        self.assertIn(f"HOME={state_dir}", " ".join(self.args("ENV")))

    def test_entrypoint_healthcheck_and_umask(self):
        self.assertEqual(json.loads(self.args("ENTRYPOINT")[0]), ["/opt/skirnir-router/container/entrypoint.sh"])
        self.assertTrue(self.args("HEALTHCHECK")[0].endswith('CMD ["python", "/opt/skirnir-router/container/healthcheck.py"]'))
        entry = read("router", "container", "entrypoint.sh")
        self.assertTrue(entry.startswith("#!/bin/sh\n"))
        self.assertRegex(entry, r"(?m)^umask 077$")
        self.assertRegex(entry, r'(?m)^exec python /opt/skirnir-router/router\.py "\$@"$')


class LockTests(unittest.TestCase):
    def test_covers_the_router_on_every_python(self):
        for py in lock_requirements.PYTHONS:
            problems = lock_requirements.check_lock(lock_requirements.CONTAINER_LOCK, [lock_requirements.ROUTER_REQS],
                                                    lock_requirements.linux_env(py), need_all=True)
            self.assertEqual(problems, [], py)

    def test_pins_the_versions_ci_tests(self):
        container, _ = lock_requirements.load(lock_requirements.CONTAINER_LOCK)
        ci, _ = lock_requirements.load(lock_requirements.CI_LOCK)
        for key, entry in container.items():
            self.assertTrue(entry["hashes"], key)
            if key in ci:
                self.assertEqual(entry["version"], ci[key]["version"], f"{key}: the image should run what CI tests")

    def test_rejects_a_missing_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "lock.txt")
            text = read("router", "requirements-container-lock.txt")
            entries, _ = pydeps.read_lock(text)
            with open(lock, "w", encoding="utf-8") as f:
                f.write("\n".join(e["line"] for k, e in entries.items() if k != "aiohttp") + "\n")
            problems = lock_requirements.check_lock(lock, [lock_requirements.ROUTER_REQS],
                                                    lock_requirements.linux_env("3.14"), need_all=True)
            self.assertTrue(any("aiohttp" in p and "no entry" in p for p in problems), problems)


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load(read("router", "compose.yaml"))
        self.router = self.compose["services"]["router"]

    def test_router_keeps_the_unit_limits(self):
        r = self.router
        self.assertEqual(r["user"], "10001:10001")
        self.assertIs(r["read_only"], True)
        self.assertEqual(r["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", r["security_opt"])
        self.assertIn("./config:/etc/skirnir-router:ro", r["volumes"])
        self.assertIn("state:/var/lib/skirnir-router", r["volumes"])
        self.assertIn("logs:/var/log/skirnir-router", r["volumes"])
        self.assertEqual(r["pull_policy"], "never")
        self.assertEqual(r["image"], "skirnir-router")

    def test_secrets_only_from_env_file(self):
        self.assertEqual(self.router["env_file"], ["secrets.env"])
        self.assertNotIn("environment", self.router)
        for name, service in self.compose["services"].items():
            env = service.get("environment") or {}
            for key in env:
                self.assertIsNone(SECRET_NAME.search(key), f"{name}: {key} belongs in an env_file")

    def test_optional_services_are_off_by_default(self):
        for name, service in self.compose["services"].items():
            if name != "router":
                self.assertTrue(service.get("profiles"), f"{name} must sit behind a profile")

    def test_healthcheck_matches_the_image(self):
        self.assertEqual(self.router["healthcheck"]["test"],
                         ["CMD", "python", "/opt/skirnir-router/container/healthcheck.py"])


class _Version(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(self.server.reply[0] if self.path == "/api/version" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.server.reply[1])

    def log_message(self, *args):
        pass


def serve(reply, tls_files=None):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Version)
    server.reply = reply
    if tls_files:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls_files)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def self_signed(folder):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "router.example.net")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    paths = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    with open(paths[0], "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(paths[1], "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return paths


class HealthcheckTests(unittest.TestCase):
    hc = load_healthcheck()

    def test_target_follows_the_router(self):
        t = self.hc.target
        self.assertEqual(t({}), ("http://127.0.0.1:11434/api/version", False))
        self.assertEqual(t({"router": {"listen": "[::]:8080"}}), ("http://[::1]:8080/api/version", False))
        self.assertEqual(t({"router": {"listen": "192.0.2.7:9000"}}), ("http://192.0.2.7:9000/api/version", False))
        tls = {"cert": "/c.pem", "key": "/k.pem"}
        self.assertEqual(t({"router": {"api_tls": True, "control_tls": tls}}), ("https://127.0.0.1:11434/api/version", True))
        # api_tls without certificate files: the router serves plain HTTP, so the probe does too
        self.assertEqual(t({"router": {"api_tls": True}}), ("http://127.0.0.1:11434/api/version", False))
        self.assertEqual(t({"router": {"listen": "0.0.0.0:http"}})[0], None)
        self.assertEqual(t({"router": {"listen": "0.0.0.0:70000"}})[0], None)

    def test_probe_reports_facts(self):
        ok = serve((200, b'{"version": "0.11.0-router0.0.0"}'))
        self.addCleanup(ok.shutdown)
        port = ok.server_address[1]
        self.assertEqual(self.hc.probe(f"http://127.0.0.1:{port}/api/version", False), (True, "0.11.0-router0.0.0"))
        healthy, reason = self.hc.probe(f"http://127.0.0.1:{port}/elsewhere", False)
        self.assertFalse(healthy)
        self.assertIn("answered 404", reason)
        other = serve((200, b"[1, 2]"))
        self.addCleanup(other.shutdown)
        healthy, reason = self.hc.probe(f"http://127.0.0.1:{other.server_address[1]}/api/version", False)
        self.assertFalse(healthy)
        self.assertIn("without a version", reason)
        ok.shutdown()
        ok.server_close()
        healthy, reason = self.hc.probe(f"http://127.0.0.1:{port}/api/version", False, timeout=2)
        self.assertFalse(healthy)
        self.assertIn("unreachable", reason)

    def test_probe_over_tls_with_the_public_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = serve((200, b'{"version": "0.11.0-router0.0.0"}'), self_signed(tmp))
            self.addCleanup(server.shutdown)
            url = f"https://127.0.0.1:{server.server_address[1]}/api/version"
            self.assertEqual(self.hc.probe(url, True), (True, "0.11.0-router0.0.0"))

    def test_main_reads_the_configuration(self):
        server = serve((200, b'{"version": "0.11.0-router0.0.0"}'))
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            self.assertEqual(self.hc.main(["healthcheck.py", path]), 1)   # missing: a fact, not a crash
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"router:\n  listen: 127.0.0.1:{server.server_address[1]}\n")
            self.assertEqual(self.hc.main(["healthcheck.py", path]), 0)
            with open(path, "w", encoding="utf-8") as f:
                f.write("router: [unclosed\n")
            self.assertEqual(self.hc.main(["healthcheck.py", path]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
