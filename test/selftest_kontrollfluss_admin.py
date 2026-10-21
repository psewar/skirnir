#!/usr/bin/env python3
"""Selbsttest (Router 0.8.11): Admin, Register, Messung, Updates und Tunnel melden erwartete Faelle als Wert (Problem).

Bis 0.8.10 warfen die Pruefungen der Admin-API ValueError (KeyError/TypeError bei fehlenden Feldern endeten als 500), das
Register und die Messung brachen per Ausnahme ab, die Agent-Anmeldung fing jede Ausnahme. Jetzt liefern sie Problem-Werte
oder pruefen vorher. Laeuft ohne Router und Netz."""
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "router"))
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
check("values.number: ganze Zahl verlangt, 3.0 ok", values.number("3.0", "x", integer=True) == 3)
check("values.number: 3.5 ist keine ganze Zahl", failed(values.number(3.5, "x", integer=True)))
check("values.number: bool zaehlt nicht", failed(values.number(True, "x")))
check("values.number: unendlich abgelehnt", failed(values.number("inf", "x")))
check("values.number: Unsinn -> Problem 400", failed(p := values.number("abc", "feld")) and p.status == 400 and "feld" in p.message)
check("values.ip_address: Normalform", values.ip_address(" 192.0.2.1 ", "x") == "192.0.2.1")
check("values.ip_address: keine IP -> Problem", failed(values.ip_address("192.0.2.300", "x")))

# --- admin: UI-Eingaben werden Problem 400 statt ValueError/KeyError/500
ok = admin._overrides_from_request({"roles": {"standard": {"tiers": [{"model": "m:1b", "num_ctx": "8192"}]}},
                                    "models": {"m:1b": {"weights_gib": "1.5", "max_parallel": 2}}})
check("admin: gueltige Rolle + Modell -> Overrides", not failed(ok) and ok["roles"]["standard"]["tiers"][0]["num_ctx"] == 8192
      and ok["models"]["m:1b"] == {"weights_gib": 1.5, "kv_gib_per_1k": 0.0, "max_parallel": 2}, msg(ok))
for what, body in (("settings ist kein Mapping", {"settings": ["x"]}),
                   ("delete_models ist keine Liste", {"delete_models": "m"}),
                   ("Tier ohne num_ctx (war KeyError -> 500)", {"roles": {"r": {"tiers": [{"model": "m"}]}}}),
                   ("num_ctx keine Zahl", {"roles": {"r": {"tiers": [{"model": "m", "num_ctx": "viel"}]}}}),
                   ("Rolle ohne Tier", {"roles": {"r": {"tiers": []}}}),
                   ("Modell ohne weights_gib (war KeyError)", {"models": {"m": {}}}),
                   ("max_parallel -1", {"models": {"m": {"weights_gib": 1, "max_parallel": -1}}}),
                   ("Client-IP ungueltig", {"clients": {"c1": {"ip": "999.1.1.1"}}}),
                   ("Client requests_per_minute keine Zahl", {"clients": {"c1": {"requests_per_minute": "viele"}}}),
                   ("Client-Name ungueltig", {"clients": {"Gross Name": {}}})):
    p = admin._overrides_from_request(body)
    check(f"admin: {what} -> Problem 400", failed(p) and p.status == 400, msg(p))
check("admin: Client mit IP-Liste", admin._client_spec("c1", {"ip": "192.0.2.1; 192.0.2.2"}) == {"ip": ["192.0.2.1", "192.0.2.2"]})
check("admin: decisions=abc -> Standard 50", admin._decisions_n(SimpleNamespace(query={"decisions": "abc"})) == 50)
check("admin: decisions=7", admin._decisions_n(SimpleNamespace(query={"decisions": "7"})) == 7)

# --- registry: Policy, Anmeldung, Datei
check("registry: Policy gueltig", registry.node_policy({"weight": "2", "busy_gpu_util_pct": 50}) == {"weight": 2, "busy_gpu_util_pct": 50.0})
check("registry: Policy weight keine Zahl -> Problem", failed(registry.node_policy({"weight": "schwer"})))
check("registry: Policy ausserhalb des Bereichs -> Problem", failed(registry.node_policy({"busy_foreign_gib": 100})))
check("registry: Policy mac ungueltig -> Problem", failed(registry.node_policy({"mac": "zz"})))


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
    check("registry: gueltiges Hello -> (pub, name, hello)", not failed(got) and got[1] == "knoten-a", msg(got))
    for what, frame in (("falsche Signatur", hello_frame(priv, nonce, sig=b"\0" * 64)),
                        ("anderer Nonce signiert", hello_frame(priv, os.urandom(32))),
                        ("Schluessel nicht 32 Bytes", hello_frame(priv, nonce, pub=b"kurz")),
                        ("kein hello", hello_frame(priv, nonce, typ="status")),
                        ("kein JSON", SimpleNamespace(type=WSMsgType.TEXT, data="{kaputt")),
                        ("kein Base64", SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({"t": "hello", "pubkey": "@@", "sig": "@@"}))),
                        ("Binaerrahmen", SimpleNamespace(type=WSMsgType.BINARY, data=b"x"))):
        p = await registry.receive_hello(FakeWS(frame), nonce)
        check(f"registry: Hello abgelehnt ({what}) -> Problem 401", failed(p) and p.status == 401, msg(p))
    p = await registry.receive_hello(FakeWS(None), nonce, timeout=0.05)
    check("registry: kein Hello in der Frist -> Problem 401", failed(p) and p.status == 401 and "hello" in p.message, msg(p))

asyncio.run(hello_cases())

tmp = tempfile.mkdtemp(prefix="skirnir-kf3-")
path = os.path.join(tmp, "nodes.json")
check("registry: nodes.json fehlt -> leeres Register", registry.NodeRegistry(path).load().nodes == {})
open(path, "w", encoding="utf-8").write("{kaputt")
check("registry: nodes.json kaputt -> leeres Register", registry.NodeRegistry(path).load().nodes == {})
open(path, "w", encoding="utf-8").write("[1, 2]")
check("registry: nodes.json kein Mapping -> leeres Register", registry.NodeRegistry(path).load().nodes == {})
open(path, "w", encoding="utf-8").write(json.dumps({"nodes": {"fp1": {"name": "a", "state": "approved"}}}))
check("registry: nodes.json gueltig", list(registry.NodeRegistry(path).load().nodes) == ["fp1"])
reg = registry.NodeRegistry(os.path.join(tmp, "fehlt", "nodes.json"))
p = reg.save()
check("registry: save in fehlendes Verzeichnis -> Problem 500, bleibt dirty", failed(p) and p.status == 500 and reg.dirty, msg(p))

# --- tunnel: Antwortkopf des Agenten
check("tunnel: Antwortkopf gueltig", tunnel.response_head(b'{"status": 200, "headers": {"a": "b"}}') == (200, {"a": "b"}))
for what, payload in (("kein JSON", b"{"), ("status fehlt", b"{}"), ("status als Text", b'{"status": "200"}'),
                      ("status bool", b'{"status": true}'), ("headers keine Mapping", b'{"status": 200, "headers": [1]}')):
    check(f"tunnel: Antwortkopf {what} -> None", tunnel.response_head(payload) is None)

# --- perf: Statistik und Knotenaufruf
check("perf: Statistik mit Text statt Zahl -> None", perf.perf_stats({"eval_count": "viel", "eval_duration": 1}, None) is None)
check("perf: Statistik kein dict -> None", perf.perf_stats([1], None) is None)
check("perf: Statistik gueltig", perf.perf_stats({"eval_count": 10, "eval_duration": 1e9}, None)["gen_tps"] == 10.0)


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
                             ("keine JSON-Antwort", FakeCM(data=[1]), 502)):
        node = SimpleNamespace(tunnel=SimpleNamespace(request=lambda *a, cm=cm, **k: cm), name="n")
        p = await perf._ollama(node, "get", "/api/ps")
        check(f"perf: Ollama-Aufruf {what} -> Problem {status}", failed(p) and p.status == status, msg(p))
    node = SimpleNamespace(tunnel=SimpleNamespace(request=lambda *a, **k: FakeCM(data={"models": "x"})), name="n")
    p = await perf._ps_models(node)
    check("perf: /api/ps models keine Liste -> Problem", failed(p), msg(p))
    # E2E-Fund 2026-10-22: Laden/Entladen per /api/generate streamt NDJSON - nur Erfolg zaehlt, nicht als JSON lesen
    node = SimpleNamespace(tunnel=SimpleNamespace(request=lambda *a, **k: FakeCM(data=None)), name="n")
    p = await perf._ollama(node, "post", "/api/generate", {"model": "m", "keep_alive": 0}, answer=False)
    check("perf: Laden/Entladen (NDJSON) -> Erfolg ohne JSON-Parse", p == {}, msg(p))

asyncio.run(ollama_cases())

# --- Updates: Fenster, Manifest, Signatur
drei_uhr, mittag = (time.mktime((2026, 10, 22, h, 0, 0, 0, 0, -1)) for h in (3, 12))
fenster = {"window_start": "02:00", "window_end": "05:00"}
check("ollamaupdate: Fenster 02:00-05:00 um 03:00", ollamaupdate.in_window(fenster, now=drei_uhr) is True)
check("ollamaupdate: Fenster 02:00-05:00 um 12:00", ollamaupdate.in_window(fenster, now=mittag) is False)
check("ollamaupdate: unlesbares Fenster = immer", ollamaupdate.in_window({"window_start": "2 Uhr", "window_end": "05:00"}) is True)
state.CFG = SimpleNamespace(path=os.path.join(tmp, "config.yaml"), agent_update={})
os.makedirs(agentupdate.agent_dir(), exist_ok=True)
mpath = os.path.join(agentupdate.agent_dir(), "manifest.json")
check("agentupdate: kein Manifest -> None", agentupdate.load_manifest() is None)
for what, content in (("kaputt", "{"), ("ohne manifest", "{}"), ("ohne files", json.dumps({"manifest": {"version": "1.0.0"}}))):
    open(mpath, "w", encoding="utf-8").write(content)
    os.utime(mpath, (len(what), len(what)))   # neue mtime: der Cache darf nicht greifen
    check(f"agentupdate: Manifest {what} -> None", agentupdate.load_manifest() is None)
priv = Ed25519PrivateKey.generate()
manifest = {"version": "1.0.0", "files": []}
open(mpath, "w", encoding="utf-8").write(json.dumps({"manifest": manifest, "signature": ""}))
os.utime(mpath, (99, 99))
check("agentupdate: gueltiges Manifest", (agentupdate.load_manifest() or {}).get("manifest") == manifest)
pub = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
sig = base64.b64encode(priv.sign(agentupdate.canonical(manifest))).decode()
check("agentupdate: Signatur passt -> None", agentupdate.verify_with(pub, manifest, sig) is None)
check("agentupdate: falsche Signatur -> Grund", agentupdate.verify_with(pub, {**manifest, "version": "2"}, sig) == "signature does not match")
check("agentupdate: kaputter Schluessel -> Grund", agentupdate.verify_with("abc", manifest, sig) == "key or signature malformed")

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
