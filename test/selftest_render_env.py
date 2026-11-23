"""render-env.sh (ADR-0104, ADR-0080): sources, reachability as a fact, every name on its own, refused names and values.

Against a fake secret store: built-in keys, EXTRA_SECRET_KEYS, no MQTT_PASSWORD at all, a store that does not answer,
refuses the login or fails one name, a store that holds none of the names, and the bearer token sent through stdin.
Against SECRET_SOURCE=file: a root-only KEY=value file is copied with the same name and value checks, never sourced;
unsafe files are refused. Needs bash, curl and jq (as on the router host); without them the test is skipped.
Synthetic values only.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "router" / "render-env.sh"
SECRETS = {"MQTT_PASSWORD": "mqtt-synthetic", "OPENAI_API_KEY": "openai-synthetic",
           "EXAMPLE_API_KEY": "example-synthetic", "OTHER_API_KEY": "other-synthetic",
           # a line break would start another variable in the root unit's EnvironmentFile
           "SKIRNIR_HANDOFF_KEY": "handoff-synthetic\nLD_PRELOAD=/tmp/injected.so",
           "LF_API_KEY": "lf-synthetic\nPATH=/tmp/injected", "CR_API_KEY": "cr-synthetic\rPYTHONPATH=/tmp/injected"}
INJECTED = ("LD_PRELOAD=", "PATH=", "PYTHONPATH=", "injected")
TOKEN = "token-synthetic"
PREVIOUS = "OPENAI_API_KEY=previous-synthetic\n"


class FakeStore(BaseHTTPRequestHandler):
    asked: list = []
    override: dict = {}
    login_status = 200
    failing: set = set()
    bad_auth = 0

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if FakeStore.login_status != 200:
            self._send(FakeStore.login_status, {"message": "synthetic refusal"})
            return
        self._send(200, {"accessToken": TOKEN})

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            FakeStore.bad_auth += 1
            self._send(401, {"message": "no token"})
            return
        name = unquote(urlparse(self.path).path.rsplit("/", 1)[-1])
        FakeStore.asked.append(name)
        secrets = {k: v for k, v in {**SECRETS, **FakeStore.override}.items() if v is not None}
        if name in FakeStore.failing:
            self._send(500, {"message": "synthetic fault"})
        elif name in secrets:
            self._send(200, {"secret": {"secretValue": secrets[name]}})
        else:
            self._send(404, {"message": "not found"})


TOOLS = all(shutil.which(t) for t in ("bash", "curl", "jq"))
if os.environ.get("CI") and not TOOLS:   # in CI a silent skip would hide that the script is not tested at all
    raise SystemExit("CI: bash, curl and jq are required for this test")


def closed_port():
    """A loopback port nobody listens on: the store that does not answer."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(TOOLS, "needs bash, curl and jq")
class RenderEnvTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeStore)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeStore.asked, FakeStore.override, FakeStore.failing = [], {}, set()
        FakeStore.login_status, FakeStore.bad_auth = 200, 0
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.conf_dir = self.base / "secrets-conf"
        self.conf_dir.mkdir(mode=0o700)
        self.conf_dir.chmod(0o700)
        self.out = self.base / "secrets.env"

    def tearDown(self):
        self.tmp.cleanup()

    def store_conf(self, port=None):
        return (f"SECRET_STORE_DOMAIN=http://127.0.0.1:{port or self.server.server_address[1]}\n"
                "SECRET_STORE_CLIENT_ID=client-synthetic\nSECRET_STORE_CLIENT_SECRET=secret-synthetic\n"
                "PROJECT_ID=project-synthetic\nENVIRONMENT=prod\n")

    def run_script(self, conf_text, previous=None):
        """(lines of secrets.env or [], whole output, STATE value)."""
        conf = self.conf_dir / "render-env.conf"
        conf.write_text(conf_text)
        conf.chmod(0o600)
        if previous is not None:
            self.out.write_text(previous)
        env = dict(os.environ, RENDER_ENV_CONF=str(conf), RENDER_ENV_OUT=str(self.out),
                   RENDER_ENV_ROUTER_WRITABLE=str(self.base / "router-writable"), RENDER_ENV_OWNER_UID=str(os.getuid()))
        for k in ("SECRET_SOURCE", "SECRET_FILE", "EXTRA_SECRET_KEYS", "SECRET_STORE_AUTH_FILE"):
            env.pop(k, None)
        run = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        log = run.stdout + run.stderr
        last = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ""
        self.assertTrue(last.startswith("STATE: "), f"the last line is the state: {log}")
        lines = self.out.read_text().splitlines() if self.out.exists() else []
        self.assertFalse([p for p in self.base.iterdir() if p.name.startswith(".secrets.env.")], "no temporary file left")
        return lines, log, last.split(": ", 1)[1]

    def assert_no_values(self, log):
        for value in list(SECRETS.values()) + [TOKEN, "secret-synthetic"]:
            self.assertNotIn(value, log, "no secret value in the output")

    # --- store -----------------------------------------------------------------------------------------------------
    def test_without_extra_keys_only_builtins(self):
        lines, log, state = self.run_script(self.store_conf())
        self.assertEqual((lines, state), (["MQTT_PASSWORD=mqtt-synthetic", "OPENAI_API_KEY=openai-synthetic"], "written"))
        self.assertNotIn("EXAMPLE_API_KEY", FakeStore.asked)
        self.assertNotIn("EXTRA_SECRET_KEYS", log)
        self.assertIn("source: secret store ready", log)
        self.assertIn("SKIRNIR_HANDOFF_KEY contains a line break - skipped", log)
        self.assertEqual(FakeStore.bad_auth, 0, "the token reaches the store as a header read from stdin")
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600, "without the router's group the file stays root's 0600")
        self.assert_no_values(log)
        lines, log, state = self.run_script(self.store_conf())
        self.assertEqual(state, "unchanged", log)

    def test_no_mqtt_password_still_renders_everything_else(self):
        """The gate this replaces: without MQTT_PASSWORD nothing at all was written (no cloud key, no DSN)."""
        FakeStore.override = {"MQTT_PASSWORD": None}
        lines, log, state = self.run_script(self.store_conf() + 'EXTRA_SECRET_KEYS="EXAMPLE_API_KEY"\n', previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=openai-synthetic", "EXAMPLE_API_KEY=example-synthetic"], "written"))
        self.assertIn("MQTT_PASSWORD: missing in the source - not rendered", log)
        self.assertNotIn("not reachable", log, "a missing value says nothing about reachability")

    def test_store_unreachable_keeps_the_file(self):
        lines, log, state = self.run_script(self.store_conf(port=closed_port()), previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "kept"))
        self.assertIn("secret store not reachable", log)
        self.assertEqual(FakeStore.asked, [], "no name is fetched from a store that is not ready")

    def test_login_refused_keeps_the_file(self):
        FakeStore.login_status = 401
        lines, log, state = self.run_script(self.store_conf(), previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "kept"))
        self.assertIn("secret store refused the login (HTTP 401", log)
        self.assertNotIn("not reachable", log)

    def test_one_failed_fetch_keeps_the_whole_file(self):
        FakeStore.failing = {"ANTHROPIC_API_KEY"}
        lines, log, state = self.run_script(self.store_conf(), previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "kept"), "a transient fault drops no secret")
        self.assertIn("ANTHROPIC_API_KEY: fetch failed (HTTP 500)", log)
        self.assertIn("SKIRNIR_HANDOFF_KEY", FakeStore.asked, "every name is still asked on its own")

    def test_store_with_none_of_the_names_keeps_the_file(self):
        FakeStore.override = {k: None for k in SECRETS}
        lines, log, state = self.run_script(self.store_conf(), previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "kept"))
        self.assertIn("holds none of the 8 names", log)

    def test_values_with_line_breaks_are_refused(self):
        lines, log, state = self.run_script(self.store_conf() + 'EXTRA_SECRET_KEYS="LF_API_KEY CR_API_KEY EXAMPLE_API_KEY"\n')
        self.assertEqual(lines, ["MQTT_PASSWORD=mqtt-synthetic", "OPENAI_API_KEY=openai-synthetic",
                                 "EXAMPLE_API_KEY=example-synthetic"])
        for name in ("LF_API_KEY", "CR_API_KEY"):
            self.assertIn(f"{name} contains a line break - skipped", log)
        for text in INJECTED:
            self.assertFalse(any(text in line for line in lines), f"{text!r} reached secrets.env")
            self.assertNotIn(text, log, "no secret value in the output")

    def test_mqtt_password_with_line_break_is_skipped_like_any_name(self):
        FakeStore.override = {"MQTT_PASSWORD": "mqtt-synthetic\nLD_PRELOAD=/tmp/injected.so"}
        lines, log, state = self.run_script(self.store_conf())
        self.assertEqual((lines, state), (["OPENAI_API_KEY=openai-synthetic"], "written"))
        self.assertIn("MQTT_PASSWORD contains a line break - skipped", log)
        self.assertNotIn("injected", log)

    def test_extra_keys_rendered_invalid_and_missing_reported(self):
        lines, log, state = self.run_script(
            self.store_conf() + 'EXTRA_SECRET_KEYS="EXAMPLE_API_KEY bad-name 9STARTS_WITH_DIGIT * MISSING_API_KEY '
            'OPENAI_API_KEY LD_PRELOAD PATH PYTHONPATH _KEY OTHER_API_KEY"\n')
        self.assertEqual(lines, ["MQTT_PASSWORD=mqtt-synthetic", "OPENAI_API_KEY=openai-synthetic",
                                 "EXAMPLE_API_KEY=example-synthetic", "OTHER_API_KEY=other-synthetic"])
        for bad in ("bad-name", "9STARTS_WITH_DIGIT", "*"):
            self.assertIn(f"'{bad}' is not a valid name - skipped", log)
            self.assertNotIn(bad, FakeStore.asked)
        # Names the loader, the shell or Python interpret must never reach the root unit's environment.
        for bad in ("LD_PRELOAD", "PATH", "PYTHONPATH"):
            self.assertIn(f"'{bad}' does not end in _KEY or _TOKEN - skipped", log)
            self.assertNotIn(bad, FakeStore.asked)
        self.assertNotIn("_KEY", FakeStore.asked)
        self.assertIn("MISSING_API_KEY: missing in the source", log)
        self.assertEqual(FakeStore.asked.count("OPENAI_API_KEY"), 1, "a built-in key named again is not fetched twice")
        self.assert_no_values(log)

    # --- configuration ---------------------------------------------------------------------------------------------
    def test_without_configuration_nothing_is_rendered(self):
        env = dict(os.environ, RENDER_ENV_CONF=str(self.conf_dir / "missing.conf"), RENDER_ENV_OUT=str(self.out))
        run = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((run.returncode, run.stdout.strip().splitlines()[-1]), (0, "STATE: not-configured"), run.stdout)
        self.assertFalse(self.out.exists())

    def test_unknown_source_is_refused(self):
        lines, log, state = self.run_script("SECRET_SOURCE=vault\n", previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "refused"))
        self.assertIn("SECRET_SOURCE 'vault' is unknown", log)

    # --- file ------------------------------------------------------------------------------------------------------
    def source_file(self, text, mode=0o600, folder=None):
        folder = folder or self.conf_dir
        path = folder / "secrets.source.env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_file_source_copies_checked_names_and_never_sources(self):
        marker = self.base / "sourced"
        src = self.source_file(
            "# synthetic\n\nOPENAI_API_KEY=openai-synthetic\n"
            f"SKIRNIR_SESSION_DSN=postgresql://127.0.0.1/db?x=$(touch {marker})\n"
            "LD_PRELOAD=/tmp/injected.so\nEXAMPLE_API_KEY=example-synthetic\nnot a line\nlower=x\n"
            "OTHER_API_KEY=first-synthetic\nOTHER_API_KEY=second-synthetic\nCR_API_KEY=cr-synthetic\r\n")
        lines, log, state = self.run_script(
            f'SECRET_SOURCE=file\nSECRET_FILE={src}\nEXTRA_SECRET_KEYS="EXAMPLE_API_KEY OTHER_API_KEY CR_API_KEY"\n')
        self.assertEqual(state, "written", log)
        self.assertEqual(lines, ["OPENAI_API_KEY=openai-synthetic",
                                 f"SKIRNIR_SESSION_DSN=postgresql://127.0.0.1/db?x=$(touch {marker})",
                                 "EXAMPLE_API_KEY=example-synthetic"], "the value is copied raw, never evaluated")
        self.assertFalse(marker.exists(), "the source file is read, never sourced")
        self.assertIn("LD_PRELOAD is neither built in nor in EXTRA_SECRET_KEYS - skipped", log)
        self.assertIn("line 7: not KEY=value - skipped", log)
        self.assertIn("line 8: not a valid name - skipped", log)
        self.assertIn("OTHER_API_KEY is given more than once - skipped", log)
        self.assertIn("CR_API_KEY contains a line break - skipped", log)
        self.assertIn("MQTT_PASSWORD: missing in the source", log)
        self.assertEqual(FakeStore.asked, [], "the file source asks no store")
        for text in ("first-synthetic", "second-synthetic", "openai-synthetic", "injected"):
            self.assertNotIn(text, log)

    def test_file_source_missing_keeps_the_file(self):
        lines, log, state = self.run_script(f"SECRET_SOURCE=file\nSECRET_FILE={self.conf_dir / 'absent.env'}\n",
                                            previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "kept"))

    def test_unsafe_source_files_are_refused(self):
        readable = self.source_file("OPENAI_API_KEY=openai-synthetic\n", mode=0o640)
        lines, log, state = self.run_script(f"SECRET_SOURCE=file\nSECRET_FILE={readable}\n", previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "refused"))
        self.assertIn("readable by group or others", log)

        writable = self.source_file("OPENAI_API_KEY=openai-synthetic\n", mode=0o620)
        lines, log, state = self.run_script(f"SECRET_SOURCE=file\nSECRET_FILE={writable}\n", previous=PREVIOUS)
        self.assertEqual(state, "refused")
        self.assertIn("writable by group or others", log)

        router_rw = self.base / "router-writable"
        router_rw.mkdir(mode=0o700)
        in_rw = self.source_file("OPENAI_API_KEY=openai-synthetic\n", folder=router_rw)
        lines, log, state = self.run_script(f"SECRET_SOURCE=file\nSECRET_FILE={in_rw}\n", previous=PREVIOUS)
        self.assertEqual((lines, state), (["OPENAI_API_KEY=previous-synthetic"], "refused"))
        self.assertIn("which the router may write", log)

        lines, log, state = self.run_script("SECRET_SOURCE=file\n", previous=PREVIOUS)
        self.assertEqual(state, "refused")
        self.assertIn("needs SECRET_FILE", log)


if __name__ == "__main__":
    unittest.main()
