#!/usr/bin/env python3
"""Self-test (Router 0.8.11): Admin, Register, Measurement, Updates, and Tunnel report expected cases as a value (Problem).

Up to 0.8.10, Admin-API checks raised ValueError (missing fields ended as 500 via KeyError/TypeError),
Register and Measurement aborted via exceptions, and Agent registration caught any exception. Now they return Problem values
or check beforehand. Runs without Router and network."""
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from aiohttp import ClientError, WSMsgType  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from skirnir_router import admin, agentupdate, ollamaupdate, perf, registry, state, tunnel, values  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def msg(p):
    return getattr(p, "message", p)


# --- values: Referenzparser an einer Stelle
check("values.number: Zahlentext", values.number("1e3", "x") == 1000.0)
check("values.number: integer required, 3.0 ok", values.number("3.0", "x", integer=True) == 3)
check("values.number: 3.5 is not an integer", failed(values.number(3.5, "x", integer=True)))
check("values.number: bool does not count", failed(values.number(True, "x")))
check("values.number: unendlich abgelehnt", failed(values.number("inf", "x")))
check("values.number: Unsinn -> Problem 400", failed(p := values.number("abc", "feld")) and p.status == 400 and "feld" in p.message)
check("values.ip_address: Normalform", values.ip_address(" 192.0.2.1 ", "x") == "192.0.2.1")
check("values.ip_address: no IP -> Problem", failed(values.ip_address("192.0.2.300", "x")))

# --- admin: UI inputs return Problem 400 instead of ValueError/KeyError/500
ok = admin._overrides_from_request({"roles": {"standard": {"tiers": [{"model": "m:1b", "num_ctx": "8192"}]}},
                                    "models": {"m:1b": {"weights_gib": "1.5", "max_parallel": 2}}})
check("admin: valid role + model -> Overrides", not failed(ok) and ok["roles"]["standard"]["tiers"][0]["num_ctx"] == 8192
      and ok["models"]["m:1b"] == {"weights_gib": 1.5, "kv_gib_per_1k": 0.0, "max_parallel": 2}, msg(ok))
for what, body in (("settings is not a mapping", {"settings": ["x"]}),
                   ("delete_models is not a list", {"delete_models": "m"}),
                   ("tier without num_ctx (was KeyError -> 500)", {"roles": {"r": {"tiers": [{"model": "m"}]}}}),
                   ("num_ctx not a number", {"roles": {"r": {"tiers": [{"model": "m", "num_ctx": "viel"}]}}}),
                   ("role without tier", {"roles": {"r": {"tiers": []}}}),
                   ("model without weights_gib (was KeyError)", {"models": {"m": {}}}),
                   ("max_parallel -1", {"models": {"m": {"weights_gib": 1, "max_parallel": -1}}}),
                   ("client IP invalid", {"clients": {"c1": {"ip": "999.1.1.1"}}}),
                   ("client requests_per_minute not a number", {"clients": {"c1": {"requests_per_minute": "viele"}}}),
                   ("client name invalid", {"clients": {"Gross Name": {}}})):
    p = admin._overrides_from_request(body)
    check(f"admin: {what} -> Problem 400", failed(p) and p.status == 400, msg(p))
check("admin: Client with IP list", admin._client_spec("c1", {"ip": "192.0.2.1; 192.0.2.2"}) == {"ip": ["192.0.2.1", "192.0.2.2"]})
check("admin: decisions=abc -> Standard 50", admin._decisions_n(SimpleNamespace(query={"decisions": "abc"})) == 50)
check("admin: decisions=7", admin._decisions_n(SimpleNamespace(query={"decisions": "7"})) == 7)

# --- registry: Policy, registration, file
check("registry: Policy valid", registry.node_policy({"weight": "2", "busy_gpu_util_pct": 50}) == {"weight": 2, "busy_gpu_util_pct": 50.0})
check("registry: Policy weight not a number -> Problem", failed(registry.node_policy({"weight": "schwer"})))
check("registry: Policy out of range -> Problem", failed(registry.node_policy({"busy_foreign_gib": 100})))
check("registry: Policy mac invalid -> Problem", failed(registry.node_policy({"mac": "zz"})))


class FakeWS:
    def __init__(self, frame=None):
        self.frame = frame

    async def receive(self):
        if self.frame is None:
            await asyncio.sleep(3600)
        return self.frame


def hello_frame(priv, nonce, name="knoten-a", sig=None, pub=None, typ="hello"):
    raw_pub = pub if pub is not None else priv.public_key().public_bytes_raw()
    signature = sig if sig is not None else priv.sign(nonce + name.encode() + raw_pub)
    body = {"t": typ, "pubkey": base64.b64encode(raw_pub).decode(), "sig": base64.b64encode(signature).decode(), "node": name}
    return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(body))


async def hello_cases():
    priv, nonce = Ed25519PrivateKey.generate(), os.urandom(32)
    got = await registry.receive_hello(FakeWS(hello_frame(priv, nonce)), nonce)
    check("registry: valid Hello -> (pub, name, hello)", not failed(got) and got[1] == "knoten-a", msg(got))
    for what, frame in (("falsche Signatur", hello_frame(priv, nonce, sig=b"\0" * 64)),
                        ("anderer Nonce signiert", hello_frame(priv, os.urandom(32))),
                        ("key not 32 bytes", hello_frame(priv, nonce, pub=b"kurz")),
                        ("no hello", hello_frame(priv, nonce, typ="status")),
                        ("no JSON", SimpleNamespace(type=WSMsgType.TEXT, data="{kaputt")),
                        ("no Base64", SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({"t": "hello", "pubkey": "@@", "sig": "@@"}))),
                        ("Binaerrahmen", SimpleNamespace(type=WSMsgType.BINARY, data=b"x"))):
        p = await registry.receive_hello(FakeWS(frame), nonce)
        check(f"registry: Hello abgelehnt ({what}) -> Problem 401", failed(p) and p.status == 401, msg(p))
    p = await registry.receive_hello(FakeWS(None), nonce, timeout=0.05)
    check("registry: no Hello in time -> Problem 401", failed(p) and p.status == 401 and "hello" in p.message, msg(p))

asyncio.run(hello_cases())

tmp = tempfile.mkdtemp(prefix="skirnir-kf3-")
path = os.path.join(tmp, "nodes.json")
check("registry: nodes.json missing -> empty registry", registry.NodeRegistry(path).load().nodes == {})
open(path, "w", encoding="utf-8").write("{kaputt")
check("registry: nodes.json corrupt -> empty registry", registry.NodeRegistry(path).load().nodes == {})
open(path, "w", encoding="utf-8").write("[1, 2]")
check("registry: nodes.json not a mapping -> empty registry", registry.NodeRegistry(path).load().nodes == {})
open(path, "w", encoding="utf-8").write(json.dumps({"nodes": {"fp1": {"name": "a", "state": "approved"}}}))
check("registry: nodes.json valid", list(registry.NodeRegistry(path).load().nodes) == ["fp1"])
reg = registry.NodeRegistry(os.path.join(tmp, "fehlt", "nodes.json"))
p = reg.save()
check("registry: save to missing directory -> Problem 500, stays dirty", failed(p) and p.status == 500 and reg.dirty, msg(p))

# --- tunnel: Agent response header
check("tunnel: response header valid", tunnel.response_head(b'{"status": 200, "headers": {"a": "b"}}') == (200, {"a": "b"}))
for what, payload in (("no JSON", b"{"), ("status missing", b"{}"), ("status als Text", b'{"status": "200"}'),
                      ("status bool", b'{"status": true}'), ("headers not a mapping", b'{"status": 200, "headers": [1]}')):
    check(f"tunnel: Antwortkopf {what} -> None", tunnel.response_head(payload) is None)

# --- perf: Statistics and node call
check("perf: statistic with text instead of number -> None", perf.perf_stats({"eval_count": "viel", "eval_duration": 1}, None) is None)
check("perf: statistic not a dict -> None", perf.perf_stats([1], None) is None)
check("perf: statistic valid", perf.perf_stats({"eval_count": 10, "eval_duration": 1e9}, None)["gen_tps"] == 10.0)


class FakeCM:
    def __init__(self, status=200, data=None, error=None):
        self.status, self.data, self.error = status, data, error

    async def __aenter__(self):
        if self.error:
            raise self.error  # exception-ok: the fake transport fails like the real one
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return "fehlertext"

    async def read(self):
        return b'{"status": "loading"}\n{"done": true}\n'   # /api/generate streamt NDJSON

    async def json(self, content_type=None):
        return self.data


async def ollama_cases():
    for what, cm, status in (("Netzfehler", FakeCM(error=ClientError("weg")), 502), ("HTTP 500", FakeCM(status=500), 502),
                             ("not a JSON answer", FakeCM(data=[1]), 502)):
        node = SimpleNamespace(tunnel=SimpleNamespace(request=lambda *a, cm=cm, **k: cm), name="n")
        p = await perf._ollama(node, "get", "/api/ps")
        check(f"perf: Ollama-Aufruf {what} -> Problem {status}", failed(p) and p.status == status, msg(p))
    node = SimpleNamespace(tunnel=SimpleNamespace(request=lambda *a, **k: FakeCM(data={"models": "x"})), name="n")
    p = await perf._ps_models(node)
    check("perf: /api/ps models not a list -> Problem", failed(p), msg(p))
    # E2E finding 2026-10-22: Load/Unload via /api/generate streams NDJSON - only success counts, do not read as JSON
    node = SimpleNamespace(tunnel=SimpleNamespace(request=lambda *a, **k: FakeCM(data=None)), name="n")
    p = await perf._ollama(node, "post", "/api/generate", {"model": "m", "keep_alive": 0}, answer=False)
    check("perf: Load/Unload (NDJSON) -> Success without JSON parse", p == {}, msg(p))

asyncio.run(ollama_cases())

# --- Updates: Fenster, Manifest, Signatur
three_am, noon = (time.mktime((2026, 10, 22, h, 0, 0, 0, 0, -1)) for h in (3, 12))
window = {"window_start": "02:00", "window_end": "05:00"}
check("ollamaupdate: Fenster 02:00-05:00 um 03:00", ollamaupdate.in_window(window, now=three_am) is True)
check("ollamaupdate: Fenster 02:00-05:00 um 12:00", ollamaupdate.in_window(window, now=noon) is False)
check("ollamaupdate: unlesbares Fenster = immer", ollamaupdate.in_window({"window_start": "2 Uhr", "window_end": "05:00"}) is True)
state.CFG = SimpleNamespace(path=os.path.join(tmp, "config.yaml"), agent_update={})
os.makedirs(agentupdate.agent_dir(), exist_ok=True)
mpath = os.path.join(agentupdate.agent_dir(), "manifest.json")
check("agentupdate: no manifest -> None", agentupdate.load_manifest() is None)
for what, content in (("kaputt", "{"), ("without manifest", "{}"), ("without files", json.dumps({"manifest": {"version": "1.0.0"}}))):
    open(mpath, "w", encoding="utf-8").write(content)
    os.utime(mpath, (len(what), len(what)))   # new mtime: cache must not hit
    check(f"agentupdate: Manifest {what} -> None", agentupdate.load_manifest() is None)
priv = Ed25519PrivateKey.generate()
manifest = {"version": "1.0.0", "files": []}
open(mpath, "w", encoding="utf-8").write(json.dumps({"manifest": manifest, "signature": ""}))
os.utime(mpath, (99, 99))
check("agentupdate: valid manifest", (agentupdate.load_manifest() or {}).get("manifest") == manifest)
pub = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
sig = base64.b64encode(priv.sign(agentupdate.canonical(manifest))).decode()
check("agentupdate: signature matches -> None", agentupdate.verify_with(pub, manifest, sig) is None)
check("agentupdate: wrong signature -> reason", agentupdate.verify_with(pub, {**manifest, "version": "2"}, sig) == "signature does not match")
check("agentupdate: corrupt key -> reason", agentupdate.verify_with("abc", manifest, sig) == "key or signature malformed")

# --- Agent update: Wait time after a failure from the `kind` field (Agent >= 0.19.1), not from the message text
SAC, SIX_H = agentupdate.APP_CONTROL_RETRY_S, 6 * 3600
blocked = 'neue Binary meldet "" statt 1.0.0: fork/exec x.new: An Application Control policy has blocked this file.'
check("retry_after: kind app_control -> 30 min", agentupdate.retry_after({"state": "failed", "kind": "app_control", "message": "egal"}) == SAC)
check("retry_after: kind empty -> 6 h, text no longer counts", agentupdate.retry_after({"state": "failed", "kind": "", "message": blocked}) == SIX_H)
check("retry_after: kind anders -> 6 h", agentupdate.retry_after({"state": "failed", "kind": "andere_art"}) == SIX_H)
check("retry_after: no kind (Agent < 0.19.1), text English -> 30 min", agentupdate.retry_after({"state": "failed", "message": blocked}) == SAC)
check("retry_after: no kind, text German -> 30 min",
      agentupdate.retry_after({"state": "failed", "message": "Diese Datei wurde von einer Anwendungssteuerungsrichtlinie blockiert."}) == SAC)
check("retry_after: no kind, other error -> 6 h", agentupdate.retry_after({"state": "failed", "message": "Download: HTTP 500"}) == SIX_H)
check("retry_after: empty order -> 6 h", agentupdate.retry_after({}) == SIX_H)

# note_report takes `kind` from the Heartbeat; a report without the field (older Agent) leaves none set
_remember, state.remember = state.remember, lambda entry: None
_reg = state.REG
state.REG = SimpleNamespace(nodes={"fp1": {"name": "n1", "update": {"version": "1.0.0", "state": "requested", "message": ""}}}, save=lambda: None)
add_node = SimpleNamespace(fp="fp1", name="n1", draining_until=0.0)
agentupdate.note_report(add_node, {"version": "1.0.0", "state": "downloading", "message": "", "kind": ""})
check("note_report: empty kind saved", state.REG.nodes["fp1"]["update"].get("kind") == "")
agentupdate.note_report(add_node, {"version": "1.0.0", "state": "failed", "message": blocked, "kind": "app_control"})
u = state.REG.nodes["fp1"]["update"]
check("note_report: kind app_control gespeichert", u.get("kind") == "app_control" and agentupdate.retry_after(u) == SAC, str(u))
agentupdate.note_report(add_node, {"version": "1.0.0", "state": "failed", "message": blocked, "kind": 7})
check("note_report: kind not text -> as if field missing", "kind" not in state.REG.nodes["fp1"]["update"], str(state.REG.nodes["fp1"]["update"]))
agentupdate.note_report(add_node, {"version": "1.0.0", "state": "failed", "message": blocked, "kind": "app_control"})
agentupdate.note_report(add_node, {"version": "1.0.0", "state": "failed", "message": "Download: HTTP 500"})
u = state.REG.nodes["fp1"]["update"]
check("note_report: report without kind removes the old one", "kind" not in u and agentupdate.retry_after(u) == SIX_H, str(u))
state.remember, state.REG = _remember, _reg

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
