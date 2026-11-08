"""Self-test (TM-15, ADR-0056): secrets and prompts never reach the log, the audit log, the journal or the overrides file,
and those files are owner-only.

Runs the real API and control apps in-process (no network, no nodes) with canary values: a wrong and a valid client
token, a wrong and a valid control password, a prompt, and a token freshly rotated through the admin API. Each canary is
searched in every captured log record, audit.jsonl, events.jsonl and roles.yaml. The test also checks that the audit log
and journal did record the events (otherwise an empty file would pass) and, on POSIX, that the files are mode 0600."""
import asyncio
import base64
import copy
import hashlib
import logging
import os
import shutil
import stat
import sys
import tempfile

import yaml
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, auth, config, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
UNIT = os.path.join(HERE, "..", "router", "skirnir-router.service")
WRONG_TOKEN = "canary-wrong-token-7f3a91"
GOOD_TOKEN = "canary-good-token-2b8e44"
WRONG_PASSWORD = "canary-wrong-password-5c1d08"
GOOD_PASSWORD = "canary-good-password-9e6f37"
PROMPT = "canary-prompt-text-4a2c6b"
FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{info}]"))
    if not cond:
        FAILS.append(name)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


async def scenarios(tmp):
    cfg_path = os.path.join(tmp, "config.yaml")
    shutil.copy(EXAMPLE, cfg_path)
    raw = copy.deepcopy(config.load_yaml(EXAMPLE))
    audit_path = os.path.join(tmp, "audit.jsonl")
    raw["router"]["client_auth"] = {"mode": "enforce", "audit_log": audit_path, "clients": {
        "canary-client": {"token_sha256": hashlib.sha256(GOOD_TOKEN.encode()).hexdigest(), "roles": ["*"], "models": True}}}
    raw["router"]["control_auth"] = {"users": {"operator": auth.hash_password(GOOD_PASSWORD, iterations=1000)}}
    # On disk, not only in memory: the token rotation reloads the configuration from this file.
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
    cfg = config.Config.load(cfg_path, use_overrides=True)
    check("configuration with canary client loads", not failed(cfg), getattr(cfg, "message", cfg))
    state.CFG, state.NODES, state.CLIENT_AUTH_MODE = cfg, {}, None
    state.DECISIONS_FILE = os.path.join(tmp, "events.jsonl")

    capture = Capture()
    capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    api, ctl = app.build_apps()
    api_client, ctl_client = TestClient(TestServer(api, host="127.0.0.1")), TestClient(TestServer(ctl, host="127.0.0.1"))
    await api_client.start_server()
    await ctl_client.start_server()
    rotated = None
    try:
        body = {"model": "standard", "messages": [{"role": "user", "content": PROMPT}], "stream": False}
        r = await api_client.post("/api/chat", json=body, headers={"Authorization": f"Bearer {WRONG_TOKEN}"})
        check("wrong client token is refused", r.status == 401, r.status)
        r = await api_client.post("/api/chat", json=body, headers={"Authorization": f"Bearer {GOOD_TOKEN}"})
        check("valid client reaches routing (no node: not 401/403)", r.status not in (401, 403), r.status)
        r = await api_client.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {GOOD_TOKEN}"})
        check("OpenAI path with a prompt is answered without a node", r.status not in (401, 403), r.status)
        r = await ctl_client.get("/admin/state", headers={"Authorization": basic("operator", WRONG_PASSWORD)})
        check("wrong control password is refused", r.status == 401, r.status)
        r = await ctl_client.post("/admin/clients/canary-client/token",
                                  headers={"Authorization": basic("operator", GOOD_PASSWORD), "X-Skirnir-UI": "1"})
        payload = await r.json() if r.status == 200 else {}
        rotated = payload.get("token")
        check("token rotation returns the new secret once", r.status == 200 and bool(rotated), r.status)
    finally:
        await api_client.close()
        await ctl_client.close()
        root.removeHandler(capture)
        root.setLevel(previous_level)

    logs = "\n".join(capture.lines)
    audit_text, events_text = read(audit_path), read(state.DECISIONS_FILE)
    overrides_path = cfg.overrides_path
    overrides_text = read(overrides_path)
    check("instrument: the log captured the requests", "AUDIT" in logs, len(capture.lines))
    check("instrument: the audit log recorded the refused token", "auth_denied" in audit_text, audit_text[:200])
    check("instrument: the audit log recorded the rotation (hash prefix only)", "client_token_rotated" in audit_text
          and "sha256_prefix" in audit_text, audit_text[-200:])
    check("instrument: the journal recorded the rotation", '"client_token": "canary-client"' in events_text, events_text[-200:])
    check("instrument: the overrides file holds the new hash", rotated is not None
          and hashlib.sha256(rotated.encode()).hexdigest() in overrides_text, overrides_path)
    canaries = {"wrong client token": WRONG_TOKEN, "valid client token": GOOD_TOKEN, "wrong control password": WRONG_PASSWORD,
                "valid control password": GOOD_PASSWORD, "prompt": PROMPT, "rotated token": rotated or "unset-rotated-token"}
    for where, text in (("log", logs), ("audit log", audit_text), ("journal", events_text), ("overrides", overrides_text)):
        for label, value in canaries.items():
            check(f"{label} not in the {where}", value not in text, where)

    if hasattr(os, "fchmod"):
        for label, path in (("audit log", audit_path), ("journal", state.DECISIONS_FILE), ("overrides", overrides_path)):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            check(f"{label} is owner-only (0600)", mode == 0o600, oct(mode))
        loose = os.path.join(tmp, "loose.jsonl")
        with open(loose, "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.chmod(loose, 0o644)
        from skirnir_router.common import open_private
        with open_private(loose, "a") as f:
            f.write("{}\n")
        check("an existing world-readable file is tightened on append", stat.S_IMODE(os.stat(loose).st_mode) == 0o600)
    else:
        print("SKIP file modes: no POSIX permission bits on this platform (CI checks them on Linux)")
    check("systemd unit sets UMask=0077", "\nUMask=0077\n" in read(UNIT))


tmp = tempfile.mkdtemp(prefix="skirnir-hygiene-")
try:
    asyncio.run(scenarios(tmp))
finally:
    state.DECISIONS_FILE = None
    shutil.rmtree(tmp, ignore_errors=True)
print(f"{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
