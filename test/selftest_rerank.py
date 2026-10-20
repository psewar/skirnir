#!/usr/bin/env python3
"""Selbsttest POST /rerank (rerank.py): TEI-Draht, Client-Recht, Knotenwahl, 424 ohne Retry-After, Antwortpruefung, Metriken.

Laeuft ohne Netz, Agent und Reranker: die Knoten tragen eine Tunnel-Attrappe, die Antworten nach Drehbuch liefert, und fuer
den Weg durch den echten Tunnel (tunnel.Tunnel) eine WebSocket-Attrappe, die die Rahmen des Agenten zurueckschickt."""
import asyncio
import copy
import json
import os
import sys
import time

from aiohttp import ClientError
from aiohttp.test_utils import TestClient, TestServer

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "router"))
from skirnir_router import app, config, metrics, nodes, poll, state, tunnel   # noqa: E402
from skirnir_router.outcome import failed   # noqa: E402
from skirnir_router.rerank_config import parse_rerank_config   # noqa: E402

FAILS = []
EXAMPLE = os.path.join(HIER, "..", "router", "config.example.yaml")
OTHER_TOKEN = "changeme"   # Beispiel-Token der Clients in config.example.yaml (home-assistant ohne Rerank-Recht)


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def load(rerank=None, client_rerank=True):
    """config.example.yaml plus rerank-Abschnitt und ein Client, der per Quell-IP (Loopback des Testservers) erkannt wird."""
    raw = copy.deepcopy(config.load_yaml(EXAMPLE))
    if rerank is not None:
        raw["router"]["rerank"] = rerank
    raw["router"]["client_auth"]["clients"]["memory-service"] = {"ip": ["127.0.0.1", "::1"], "rerank": client_rerank}
    cfg = config.Config(EXAMPLE, use_overrides=False)
    cfg.parse(raw)
    return cfg


# --- Konfiguration
d = parse_rerank_config(None)
check("default: aus, keine Knoten, 2 s, busy_ok, 512 Texte, 4 MiB",
      not d.enabled and d.nodes == () and d.timeout_s == 2.0 and d.busy_ok and d.max_texts == 512 and d.max_body_bytes == 4194304)
for bad in ({"enabled": True}, {"enabled": "yes", "nodes": ["a"]}, {"nodes": "a"}, {"nodes": ["a", "a"]}, {"timeout_s": 0},
            {"timeout_s": float("nan")}, {"max_texts": 0}, {"max_body_bytes": True}, {"busy_ok": 1}, {"retries": 3}):
    check(f"ungueltig abgelehnt: {bad}", failed(parse_rerank_config(bad)))
check("Config ohne rerank-Abschnitt laedt, Endpunkt aus", not config.Config(EXAMPLE, use_overrides=False).rerank.enabled)
for raw, what in (({"enabled": True}, "enabled ohne Knoten"), ({"enable": True}, "Tippfehler im Schluessel")):
    try:
        load(raw)
        ok = False
    except ValueError:   # Config.parse bricht das Laden einer ungueltigen Konfiguration mit ValueError ab
        ok = True
    check(f"Config laedt nicht: {what}", ok)
try:
    load({"enabled": True, "nodes": ["gpu-a"]}, client_rerank="yes")
    ok = False
except ValueError:
    ok = True
check("Config laedt nicht: Client-Recht rerank ist kein Boolean", ok)


# --- Heartbeat-Feld routes
check("aelterer Agent ohne routes: leer", poll.route_health(None) == {})
check("routes uebernommen, Grund leer wenn up",
      poll.route_health({"rerank": {"up": True, "reason": "x"}}) == {"rerank": {"up": True, "reason": ""}})
check("up muss ein echter Boolean sein, unbekannter Grund wird unhealthy",
      poll.route_health({"rerank": {"up": "true", "reason": "neu"}}) == {"rerank": {"up": False, "reason": "unhealthy"}})
check("bekannter Grund bleibt", poll.route_health({"rerank": {"up": False, "reason": "starting"}})["rerank"]["reason"] == "starting")
state.CFG = load()
poll.evaluate = lambda node, now: None   # der Zustandsautomat ist hier nicht Gegenstand
hb_node = nodes.Node("gpu-hb", {})
poll.apply_heartbeat(hb_node, {"routes": {"rerank": {"up": True, "reason": ""}}}, time.time())
check("apply_heartbeat setzt routes", hb_node.routes == {"rerank": {"up": True, "reason": ""}})
poll.apply_heartbeat(hb_node, {}, time.time())
check("Heartbeat ohne routes loescht sie wieder", hb_node.routes == {})


# --- Tunnel-Attrappe
class FakeResp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def read(self):
        return self.body


class FakeCall:
    def __init__(self, tun, method, path, kw):
        self.tun, self.method, self.path, self.kw = tun, method, path, kw

    async def __aenter__(self):
        self.tun.calls.append((self.method, self.path, self.kw.get("json")))
        mode = self.tun.mode
        if mode == "hang":
            await asyncio.sleep(30)
        if mode == "error":
            raise ClientError("tunnel weg")
        body = self.kw["json"]
        n = len(body["texts"])
        # Score = Anteil gemeinsamer Woerter mit der Frage; absichtlich unsortiert geliefert (Router sortiert nach Pruefung)
        q = set(body["query"].split())
        rows = [{"index": i, "score": len(q & set(t.split())) / (1 + len(q))} for i, t in enumerate(body["texts"])]
        if mode == "short":
            rows = rows[:-1]
        elif mode == "nan":
            rows[0]["score"] = float("nan")
        elif mode == "dup":
            rows[-1]["index"] = 0
        elif mode == "boolindex":
            rows[0]["index"] = True
        elif mode == "notjson":
            return FakeResp(200, b"<html>")
        elif mode == "status500":
            return FakeResp(500, b"{}")
        assert n == len(body["texts"])
        return FakeResp(200, json.dumps(rows).encode())

    async def __aexit__(self, *exc):
        return False


class FakeTunnel:
    def __init__(self):
        self.calls, self.mode = [], "ok"

    def request(self, method, path, **kw):
        return FakeCall(self, method, path, kw)


def node(name, up=True, routes=True, busy=False, tun=True, hb=True):
    n = nodes.Node(name, {})
    n.hb_ts = time.time() if hb else 0.0
    n.tunnel = FakeTunnel() if tun else None
    n.routes = {"rerank": {"up": up, "reason": "" if up else "unhealthy"}} if routes else {}
    n.state = "busy" if busy else "free"
    return n


QUERY = {"query": "wann laeuft der rasenmaeher", "texts": ["der rasenmaeher laeuft um neun", "heizung", "wann kommt der bus"]}


async def post(client, body=QUERY, token=None, raw=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await client.post("/rerank", data=raw if raw is not None else json.dumps(body), headers=headers)
    return r.status, (await r.json(content_type=None)), r.headers


async def info(client, token=None):
    r = await client.get("/info", headers={"Authorization": f"Bearer {token}"} if token else {})
    return r.status, (await r.json(content_type=None))


async def scenarios():
    state.METRICS = {"counters": {}, "hist": {}}
    state.CFG = load()
    state.NODES = {}
    api, _ = app.build_apps()
    client = TestClient(TestServer(api, host="127.0.0.1"))
    await client.start_server()
    try:
        s, b, _ = await post(client)
        check("rerank aus (default): 404", s == 404 and b["error"]["code"] == "disabled", f"{s} {b}")
        s, b = await info(client)
        check("GET /info bei rerank aus: 404", s == 404 and b["error"]["code"] == "disabled", f"{s} {b}")

        state.CFG = load({"enabled": True, "nodes": ["gpu-a", "gpu-b"], "timeout_s": 0.5, "busy_ok": False, "max_texts": 4,
                          "max_body_bytes": 2048})
        a = node("gpu-a")
        state.NODES = {"gpu-a": a}
        s, b, _ = await post(client, token=OTHER_TOKEN)
        check("Client ohne Recht rerank: 403", s == 403 and b["error"]["code"] == "forbidden", f"{s} {b}")
        check("abgelehnter Client erreicht keinen Knoten", a.tunnel.calls == [])
        s, b = await info(client, token=OTHER_TOKEN)
        check("GET /info ohne Recht: 403", s == 403 and b["error"]["code"] == "forbidden", f"{s} {b}")
        s, b = await info(client)
        check("GET /info mit Recht: 200 mit model_id (Hindsights TEI-Client braucht es beim Initialisieren)",
              s == 200 and b == {"model_id": "skirnir-rerank", "max_client_batch_size": 4}, f"{s} {b}")
        state.NODES = {}
        s, b = await info(client)
        check("GET /info haengt nicht am Knotenzustand (kein Knoten da: trotzdem 200, kein Knotenaufruf)",
              s == 200 and b.get("model_id") == "skirnir-rerank" and a.tunnel.calls == [], f"{s} {b}")
        state.NODES = {"gpu-a": a}

        s, b, h = await post(client)
        check("Client per Quell-IP mit Recht: 200", s == 200, f"{s} {b}")
        check("ein Eintrag je Text, absteigend nach Score, nur index und score",
              s == 200 and sorted(e["index"] for e in b) == [0, 1, 2] and [e["score"] for e in b] == sorted((e["score"] for e in b), reverse=True)
              and all(set(e) == {"index", "score"} for e in b) and b[0]["index"] == 0, str(b))
        check("genau ein Aufruf, POST /rerank, nur query und texts weitergereicht",
              a.tunnel.calls == [("POST", "/rerank", {"query": QUERY["query"], "texts": QUERY["texts"]})], str(a.tunnel.calls))

        s, b, _ = await post(client, {**QUERY, "return_text": True, "raw_scores": False, "truncate": True})
        check("return_text: Text je Eintrag, weitere TEI-Felder ignoriert", s == 200 and all(e["text"] == QUERY["texts"][e["index"]] for e in b), str(b))

        for body, raw, want, what in ((None, b"kein json", 400, "kein JSON"), ({"query": "q", "texts": []}, None, 400, "leere texts"),
                                      ({"query": "", "texts": ["a"]}, None, 400, "leere query"), ({"query": "q", "texts": ["a", 1]}, None, 400, "Text keine Zeichenkette"),
                                      ({"query": "q", "texts": ["a"], "return_text": "ja"}, None, 400, "return_text kein Boolean"),
                                      ({"query": "q", "texts": ["a"] * 5}, None, 413, "mehr als max_texts"),
                                      ({"query": "q", "texts": ["a" * 3000]}, None, 413, "Body ueber max_body_bytes")):
            calls = len(a.tunnel.calls)
            s, b, _ = await post(client, body, raw=raw)
            check(f"Eingabe {what}: {want}, kein Knotenaufruf", s == want and len(a.tunnel.calls) == calls, f"{s} {b}")

        async def unavailable(reason, what):
            s, b, h = await post(client)
            check(f"{what}: 424 {reason} ohne Retry-After", s == 424 and b == {"error": {"code": "rerank_unavailable", "reason": reason}}
                  and "Retry-After" not in h, f"{s} {b} {dict(h)}")

        state.NODES = {"gpu-a": node("gpu-a", hb=False)}
        await unavailable("node_offline", "Agent meldet sich nicht")
        state.NODES = {"gpu-a": node("gpu-a", tun=False)}
        await unavailable("tunnel_down", "kein Tunnel")
        state.NODES = {"gpu-a": node("gpu-a", routes=False)}
        await unavailable("reranker_down", "aelterer Agent ohne routes")
        state.NODES = {"gpu-a": node("gpu-a", up=False)}
        await unavailable("reranker_down", "Route meldet up=false")
        state.NODES = {"gpu-a": node("gpu-a", busy=True)}
        await unavailable("gpu_busy", "Knoten busy, busy_ok false")
        state.NODES = {}
        await unavailable("disabled_node", "Knoten unbekannt oder nicht freigegeben")
        b_node = node("gpu-b")
        state.NODES = {"gpu-a": node("gpu-a", up=False), "gpu-b": b_node}
        s, _, _ = await post(client)
        check("erster Knoten ohne Reranker, zweiter dient", s == 200 and len(b_node.tunnel.calls) == 1)

        state.CFG = load({"enabled": True, "nodes": ["gpu-a"], "timeout_s": 0.5, "busy_ok": True})
        busy = node("gpu-a", busy=True)
        state.NODES = {"gpu-a": busy}
        s, _, _ = await post(client)
        check("busy_ok true: busy-Knoten dient", s == 200 and len(busy.tunnel.calls) == 1)

        for mode, reason in (("short", "upstream_invalid"), ("nan", "upstream_invalid"), ("dup", "upstream_invalid"),
                             ("boolindex", "upstream_invalid"), ("notjson", "upstream_invalid"), ("status500", "upstream_error"),
                             ("error", "upstream_error")):
            n = node("gpu-a")
            n.tunnel.mode = mode
            state.NODES = {"gpu-a": n}
            await unavailable(reason, f"Knoten antwortet {mode}")
            check(f"{mode}: kein zweiter Versuch", len(n.tunnel.calls) == 1)

        n = node("gpu-a")
        n.tunnel.mode = "hang"
        state.NODES = {"gpu-a": n}
        t0 = time.monotonic()
        await unavailable("timeout", "Knoten haengt")
        dt = time.monotonic() - t0
        check("Zeitgrenze timeout_s haelt (0,5 s), ein Versuch", 0.4 <= dt < 2.0 and len(n.tunnel.calls) == 1, f"{dt:.2f} s")

        # Durch den echten Tunnel: Rahmen REQ hin, RESP/DATA/END zurueck (wie agent/tunnel.go)
        class FakeWS:
            def __init__(self, answer):
                self.answer, self.seen = answer, []

            async def send_bytes(self, frame):
                if frame[0] != tunnel.TUN_REQ:
                    return
                sid = int.from_bytes(frame[1:5], "big")
                head, _, body = frame[5:].partition(b"\n")
                self.seen.append((json.loads(head), json.loads(body)))
                if self.answer is None:
                    return   # Agent schweigt: die Frist des Tunnels muss greifen
                loop = asyncio.get_running_loop()
                loop.call_soon(real.dispatch, tunnel.tun_frame(tunnel.TUN_RESP, sid, json.dumps({"status": 200, "headers": {}}).encode()))
                loop.call_soon(real.dispatch, tunnel.tun_frame(tunnel.TUN_DATA, sid, self.answer))
                loop.call_soon(real.dispatch, tunnel.tun_frame(tunnel.TUN_END, sid))

        n = node("gpu-a")
        ws = FakeWS(json.dumps([{"index": 1, "score": 0.2}, {"index": 0, "score": 0.9}, {"index": 2, "score": 0.1}]).encode())
        real = tunnel.Tunnel(n, ws, "127.0.0.1")
        n.tunnel = real
        state.NODES = {"gpu-a": n}
        s, b, _ = await post(client)
        check("echter Tunnel: 200, sortiert", s == 200 and [e["index"] for e in b] == [0, 1, 2], f"{s} {b}")
        check("echter Tunnel: Kopf POST /rerank mit JSON, Body nur query und texts",
              ws.seen and ws.seen[0][0]["method"] == "POST" and ws.seen[0][0]["path"] == "/rerank"
              and ws.seen[0][1] == {"query": QUERY["query"], "texts": QUERY["texts"]}, str(ws.seen))
        ws.answer = None
        t0 = time.monotonic()
        await unavailable("timeout", "echter Tunnel, Agent schweigt")
        check("echter Tunnel: Frist haelt, Stream aufgeraeumt", time.monotonic() - t0 < 2.0 and not real.streams, f"{real.streams}")

        c = state.METRICS["counters"].get("skirnir_rerank_requests_total", {})
        got = {dict(k)["outcome"]: v for k, v in c.items()}
        check("Metrik skirnir_rerank_requests_total je outcome",
              got.get("ok", 0) >= 5 and got.get("timeout") == 2 and got.get("forbidden") == 1 and got.get("disabled") == 1
              and got.get("reranker_down") == 2 and got.get("too_large") == 2, str(got))
        h = state.METRICS["hist"].get("skirnir_rerank_duration_seconds", {"series": {}})
        check("Dauer als Histogramm je outcome", any(dict(k)["outcome"] == "ok" for k in h["series"]), str(list(h["series"])))
        text = metrics.render()
        check("/metrics zeigt beide Reihen", 'skirnir_rerank_requests_total{outcome="timeout"} 2' in text
              and "skirnir_rerank_duration_seconds_bucket" in text)
    finally:
        await client.close()


asyncio.run(scenarios())
print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
