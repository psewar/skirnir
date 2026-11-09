"""render-env.sh against a fake secret store: built-in keys, EXTRA_SECRET_KEYS (ADR-0080), invalid and missing names.

Needs bash, curl and jq (as on the router host); without them the test is skipped. Synthetic values only.
"""
import json
import os
from pathlib import Path
import shutil
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


class FakeStore(BaseHTTPRequestHandler):
    asked: list = []
    override: dict = {}

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
        self._send(200, {"accessToken": "token-synthetic"})

    def do_GET(self):
        name = unquote(urlparse(self.path).path.rsplit("/", 1)[-1])
        FakeStore.asked.append(name)
        secrets = {**SECRETS, **FakeStore.override}
        if name in secrets:
            self._send(200, {"secret": {"secretValue": secrets[name]}})
        else:
            self._send(404, {"message": "not found"})


TOOLS = all(shutil.which(t) for t in ("bash", "curl", "jq"))
if os.environ.get("CI") and not TOOLS:   # in CI a silent skip would hide that the script is not tested at all
    raise SystemExit("CI: bash, curl and jq are required for this test")


@unittest.skipUnless(TOOLS, "needs bash, curl and jq")
class RenderEnvTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeStore)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def render(self, extra_line, override=None):
        FakeStore.asked = []
        FakeStore.override = override or {}
        with tempfile.TemporaryDirectory() as tmp:
            conf_dir = Path(tmp) / "secrets-conf"
            conf_dir.mkdir(mode=0o700)
            conf = conf_dir / "render-env.conf"
            conf.write_text(
                f"SECRET_STORE_DOMAIN=http://127.0.0.1:{self.server.server_address[1]}\n"
                "SECRET_STORE_CLIENT_ID=client-synthetic\nSECRET_STORE_CLIENT_SECRET=secret-synthetic\n"
                "PROJECT_ID=project-synthetic\nENVIRONMENT=prod\n" + extra_line)
            conf.chmod(0o600)
            out = Path(tmp) / "secrets.env"
            env = dict(os.environ, RENDER_ENV_CONF=str(conf), RENDER_ENV_OUT=str(out),
                       RENDER_ENV_ROUTER_WRITABLE=str(Path(tmp) / "router-writable"), RENDER_ENV_OWNER_UID=str(os.getuid()))
            run = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            lines = out.read_text().splitlines() if out.exists() else []
            return lines, run.stdout + run.stderr

    def test_without_extra_keys_only_builtins(self):
        lines, log = self.render("")
        self.assertEqual(lines, ["MQTT_PASSWORD=mqtt-synthetic", "OPENAI_API_KEY=openai-synthetic"])
        self.assertNotIn("EXAMPLE_API_KEY", FakeStore.asked)
        self.assertNotIn("EXTRA_SECRET_KEYS", log)
        self.assertIn("SKIRNIR_HANDOFF_KEY contains a line break - skipped", log)

    def test_values_with_line_breaks_are_refused(self):
        lines, log = self.render('EXTRA_SECRET_KEYS="LF_API_KEY CR_API_KEY EXAMPLE_API_KEY"\n')
        self.assertEqual(lines, ["MQTT_PASSWORD=mqtt-synthetic", "OPENAI_API_KEY=openai-synthetic",
                                 "EXAMPLE_API_KEY=example-synthetic"])
        for name in ("LF_API_KEY", "CR_API_KEY"):
            self.assertIn(f"EXTRA_SECRET_KEYS: {name} contains a line break - skipped", log)
        for text in INJECTED:
            self.assertFalse(any(text in line for line in lines), f"{text!r} reached secrets.env")
            self.assertNotIn(text, log, "no secret value in the output")

    def test_mqtt_password_with_line_break_renders_nothing(self):
        lines, log = self.render("", override={"MQTT_PASSWORD": "mqtt-synthetic\nLD_PRELOAD=/tmp/injected.so"})
        self.assertEqual(lines, [], "the existing secrets.env stays; here none exists, so none is written")
        self.assertIn("MQTT_PASSWORD contains a line break - not rendered", log)

    def test_extra_keys_rendered_invalid_and_missing_reported(self):
        lines, log = self.render('EXTRA_SECRET_KEYS="EXAMPLE_API_KEY bad-name 9STARTS_WITH_DIGIT * MISSING_API_KEY '
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
        self.assertIn("MISSING_API_KEY missing in the secret store", log)
        self.assertEqual(FakeStore.asked.count("OPENAI_API_KEY"), 1, "a built-in key named again is not fetched twice")
        for value in SECRETS.values():
            self.assertNotIn(value, log, "no secret value in the output")


if __name__ == "__main__":
    unittest.main()
