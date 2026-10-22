#!/usr/bin/env python3
"""Self-test POST /rerank (rerank.py): TEI wire, client permissions, node selection, 424 without Retry-After, response validation, metrics.

Runs without network, agent, or reranker: nodes carry a tunnel stub that serves scripted responses, and a WebSocket stub for the real tunnel path (tunnel.Tunnel) that echoes agent frames."""
import asyncio
import copy
import json
import logging
import os
import sys
import time

from aiohttp import ClientError
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, config, metrics, nodes, poll, state, tunnel   # noqa: E402
from skirnir_router.outcome import failed   # noqa: E402
from skirnir_router.rerank_config import parse_rerank_config   # noqa: E402

FAILS = []
EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
OTHER_TOKEN = "changeme"   # Example client tokens in config.example.yaml (home-assistant without rerank permission)


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def load(rerank=None, client_rerank=True, inference=None):
    """config.example.yaml plus rerank section and a client identified by source IP (test server loopback)."""
    raw = copy.deepcopy(config.load_yaml(EXAMPLE))
    if rerank is not None:
        raw["router"]["rerank"] = rerank
    raw["router"]["client_auth"]["clients"]["memory-service"] = {"ip": ["127.0.0.1", "::1"], "rerank": client_rerank}
    if inference is not None:
        raw["router"]["client_auth"]["clients"]["memory-service"]["inference"] = inference
    cfg = config.Config(EXAMPLE, use_overrides=False)
    problem = cfg.parse(raw)   # 0.8.9: invalid = Problem instead of ValueError
    return problem if failed(problem) else cfg


# --- Konfiguration
d = parse_rerank_config(None)
check("default: off, no nodes, 2 s, busy_ok, 512 texts, 4 MiB",
      not d.enabled and d.nodes == () and d.timeout_s == 2.0 and d.busy_ok and d.max_texts == 512 and d.max_body_bytes == 4194304)
for bad in ({"enabled": True}, {"enabled": "yes", "nodes": ["a"]}, {"nodes": "a"}, {"nodes": ["a", "a"]}, {"timeout_s": 0},
            {"timeout_s": float("nan")}, {"max_texts": 0}, {"max_body_bytes": True}, {"busy_ok": 1}, {"retries": 3}):
    check(f"invalid rejected: {bad}", failed(parse_rerank_config(bad)))
check("Config without rerank section loads, endpoint off", not config.Config(EXAMPLE, use_overrides=False).rerank.enabled)
for raw, what in (({"enabled": True}, "enabled without nodes"), ({"enable": True}, "typo in the key")):
    check(f"Config does not load: {what}", failed(load(raw)))
check("Config does not load: Client right rerank is not a Boolean", failed(load({"enabled": True, "nodes": ["gpu-a"]}, client_rerank="yes")))
check("Config does not load: inference is not a Boolean", failed(load({"enabled": True, "nodes": ["gpu-a"]}, inference="nein")))


class Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.out_lines = []

    def emit(self, record):
        self.out_lines.append(record.getMessage())


w = Warnings()
logging.getLogger("router").addHandler(w)
load({"enabled": True, "nodes": ["gpu-a"]})
check("Warning on load: rerank without inference: false and without roles limit",
      any("memory-service" in z and "inference: false" in z for z in w.out_lines), str(w.out_lines))
w.out_lines.clear()
load({"enabled": True, "nodes": ["gpu-a"]}, inference=False)
check("no warning with inference: false", not any("memory-service" in z for z in w.out_lines), str(w.out_lines))
check("inference default true, false stays false",
      load().client_auth["clients"]["memory-service"]["inference"] is True
      and load(inference=False).client_auth["clients"]["memory-service"]["inference"] is False)
logging.getLogger("router").removeHandler(w)


# --- Heartbeat-Feld routes
check("older agent without routes: empty", poll.route_health(None) == {})
check("routes adopted, base empty if up",
      poll.route_health({"rerank": {"up": True, "reason": "x"}}) == {"rerank": {"up": True, "reason": ""}})
check("up must be a real Boolean, unknown reason becomes unhealthy",
      poll.route_health({"rerank": {"up": "true", "reason": "neu"}}) == {"rerank": {"up": False, "reason": "unhealthy"}})
check("known reason remains", poll.route_health({"rerank": {"up": False, "reason": "starting"}})["rerank"]["reason"] == "starting")
state.CFG = load()
poll.evaluate = lambda node, now: None   # the state machine is not the subject here
hb_node = nodes.Node("gpu-hb", {})
poll.apply_heartbeat(hb_node, {"routes": {"rerank": {"up": True, "reason": ""}}}, time.time())
check("apply_heartbeat setzt routes", hb_node.routes == {"rerank": {"up": True, "reason": ""}})
poll.apply_heartbeat(hb_node, {}, time.time())
check("Heartbeat without routes deletes them again", hb_node.routes == {})


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
            raise ClientError("tunnel gone")
        body = self.kw["json"]
        n = len(body["texts"])
        # Score = fraction of words shared with the question; intentionally unsorted (router sorts after check)
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
        check("rerank off (default): 404", s == 404 and b["error"]["code"] == "disabled", f"{s} {b}")
        s, b = await info(client)
        check("GET /info with rerank off: 404", s == 404 and b["error"]["code"] == "disabled", f"{s} {b}")

        state.CFG = load({"enabled": True, "nodes": ["gpu-a", "gpu-b"], "timeout_s": 0.5, "busy_ok": False, "max_texts": 4,
                          "max_body_bytes": 2048})
        a = node("gpu-a")
        state.NODES = {"gpu-a": a}
        s, b, _ = await post(client, token=OTHER_TOKEN)
        check("Client without right rerank: 403", s == 403 and b["error"]["code"] == "forbidden", f"{s} {b}")
        check("rejected client reaches no node", a.tunnel.calls == [])
        s, b = await info(client, token=OTHER_TOKEN)
        check("GET /info without right: 403", s == 403 and b["error"]["code"] == "forbidden", f"{s} {b}")
        s, b = await info(client)
        check("GET /info with right: 200 with model_id (Hindsights TEI-Client needs it when initializing)",
              s == 200 and b == {"model_id": "skirnir-rerank", "max_client_batch_size": 4}, f"{s} {b}")
        state.NODES = {}
        s, b = await info(client)
        check("GET /info does not depend on node state (no node there: still 200, no node call)",
              s == 200 and b.get("model_id") == "skirnir-rerank" and a.tunnel.calls == [], f"{s} {b}")
        state.NODES = {"gpu-a": a}

        s, b, h = await post(client)
        check("Client by source IP with right: 200", s == 200, f"{s} {b}")
        check("one entry per text, descending by score, only index and score",
              s == 200 and sorted(e["index"] for e in b) == [0, 1, 2] and [e["score"] for e in b] == sorted((e["score"] for e in b), reverse=True)
              and all(set(e) == {"index", "score"} for e in b) and b[0]["index"] == 0, str(b))
        check("exactly one call, POST /rerank, only query and texts forwarded",
              a.tunnel.calls == [("POST", "/rerank", {"query": QUERY["query"], "texts": QUERY["texts"]})], str(a.tunnel.calls))

        s, b, _ = await post(client, {**QUERY, "return_text": True, "raw_scores": False, "truncate": True})
        check("return_text: Text per entry, other TEI fields ignored", s == 200 and all(e["text"] == QUERY["texts"][e["index"]] for e in b), str(b))

        for body, raw, want, what in ((None, b"kein json", 400, "no JSON"), ({"query": "q", "texts": []}, None, 400, "leere texts"),
                                      ({"query": "", "texts": ["a"]}, None, 400, "leere query"), ({"query": "q", "texts": ["a", 1]}, None, 400, "text not a string"),
                                      ({"query": "q", "texts": ["a"], "return_text": "ja"}, None, 400, "return_text not a boolean"),
                                      ({"query": "q", "texts": ["a"] * 5}, None, 413, "mehr als max_texts"),
                                      ({"query": "q", "texts": ["a" * 3000]}, None, 413, "body over max_body_bytes")):
            calls = len(a.tunnel.calls)
            s, b, _ = await post(client, body, raw=raw)
            check(f"Input {what}: {want}, no node call", s == want and len(a.tunnel.calls) == calls, f"{s} {b}")

        async def unavailable(reason, what):
            s, b, h = await post(client)
            check(f"{what}: 424 {reason} without Retry-After", s == 424 and b == {"error": {"code": "rerank_unavailable", "reason": reason}}
                  and "Retry-After" not in h, f"{s} {b} {dict(h)}")

        state.NODES = {"gpu-a": node("gpu-a", hb=False)}
        await unavailable("node_offline", "agent does not respond")
        state.NODES = {"gpu-a": node("gpu-a", tun=False)}
        await unavailable("tunnel_down", "no tunnel")
        state.NODES = {"gpu-a": node("gpu-a", routes=False)}
        await unavailable("reranker_down", "older agent without routes")
        state.NODES = {"gpu-a": node("gpu-a", up=False)}
        await unavailable("reranker_down", "Route meldet up=false")
        state.NODES = {"gpu-a": node("gpu-a", busy=True)}
        await unavailable("gpu_busy", "node busy, busy_ok false")
        state.NODES = {}
        await unavailable("disabled_node", "node unknown or not approved")
        b_node = node("gpu-b")
        state.NODES = {"gpu-a": node("gpu-a", up=False), "gpu-b": b_node}
        s, _, _ = await post(client)
        check("first node without Reranker, second serves", s == 200 and len(b_node.tunnel.calls) == 1)

        state.CFG = load({"enabled": True, "nodes": ["gpu-a"], "timeout_s": 0.5, "busy_ok": True})
        busy = node("gpu-a", busy=True)
        state.NODES = {"gpu-a": busy}
        s, _, _ = await post(client)
        check("busy_ok true: busy node serves", s == 200 and len(busy.tunnel.calls) == 1)

        for mode, reason in (("short", "upstream_invalid"), ("nan", "upstream_invalid"), ("dup", "upstream_invalid"),
                             ("boolindex", "upstream_invalid"), ("notjson", "upstream_invalid"), ("status500", "upstream_error"),
                             ("error", "upstream_error")):
            n = node("gpu-a")
            n.tunnel.mode = mode
            state.NODES = {"gpu-a": n}
            await unavailable(reason, f"Knoten antwortet {mode}")
            check(f"{mode}: no second attempt", len(n.tunnel.calls) == 1)

        n = node("gpu-a")
        n.tunnel.mode = "hang"
        state.NODES = {"gpu-a": n}
        t0 = time.monotonic()
        await unavailable("timeout", "node hangs")
        dt = time.monotonic() - t0
        check("Time limit timeout_s holds (0.5 s), one attempt", 0.4 <= dt < 2.0 and len(n.tunnel.calls) == 1, f"{dt:.2f} s")

        # Through the real tunnel: REQ frame out, RESP/DATA/END back (like agent/tunnel.go)
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
                    return   # Agent silent: tunnel timeout must trigger
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
        check("real tunnel: HEAD POST /rerank with JSON, Body only query and texts",
              ws.seen and ws.seen[0][0]["method"] == "POST" and ws.seen[0][0]["path"] == "/rerank"
              and ws.seen[0][1] == {"query": QUERY["query"], "texts": QUERY["texts"]}, str(ws.seen))
        ws.answer = None
        t0 = time.monotonic()
        await unavailable("timeout", "echter Tunnel, Agent schweigt")
        check("real tunnel: deadline holds, Stream cleaned up", time.monotonic() - t0 < 2.0 and not real.streams, f"{real.streams}")

        c = state.METRICS["counters"].get("skirnir_rerank_requests_total", {})
        got = {dict(k)["outcome"]: v for k, v in c.items()}
        check("Metric skirnir_rerank_requests_total per outcome",
              got.get("ok", 0) >= 5 and got.get("timeout") == 2 and got.get("forbidden") == 1 and got.get("disabled") == 1
              and got.get("reranker_down") == 2 and got.get("too_large") == 2, str(got))
        h = state.METRICS["hist"].get("skirnir_rerank_duration_seconds", {"series": {}})
        check("Duration as histogram per outcome", any(dict(k)["outcome"] == "ok" for k in h["series"]), str(list(h["series"])))
        text = metrics.render()
        check("/metrics shows both series", 'skirnir_rerank_requests_total{outcome="timeout"} 2' in text
              and "skirnir_rerank_duration_seconds_bucket" in text)

        # --- Client with inference: false: only POST /rerank and GET /info, everything else 403 inference_disabled
        s, _ = await info(client)
        r = await client.get("/api/tags")
        check("inference Standard (true): Client may continue Inference endpoints", s == 200 and r.status == 200, f"{s} {r.status}")
        state.CFG = load({"enabled": True, "nodes": ["gpu-a"], "timeout_s": 0.5}, inference=False)
        n = node("gpu-a")
        state.NODES = {"gpu-a": n}
        s, b, _ = await post(client)
        check("inference false: POST /rerank weiter 200", s == 200 and len(n.tunnel.calls) == 1, f"{s} {b}")
        s, b = await info(client)
        check("inference false: GET /info weiter 200", s == 200 and b.get("model_id") == "skirnir-rerank", f"{s} {b}")
        chat = {"model": "standard:latest", "messages": [{"role": "user", "content": "hallo"}]}
        for method, path, body, code_of in (
                ("POST", "/api/chat", chat, lambda b: b.get("code")),
                ("GET", "/api/tags", None, lambda b: b.get("code")),
                ("POST", "/v1/chat/completions", chat, lambda b: b["error"].get("code")),
                ("POST", "/v1/messages", {**chat, "max_tokens": 8}, lambda b: b["error"]["skirnir"]["code"]),
                ("POST", "/v1/skirnir/memory/recall", {"project": "p", "query": "q"}, lambda b: b["error"].get("code")),
                ("GET", "/v1/skirnir/sessions", None, lambda b: b["error"].get("code")),
                ("GET", "/rerank", None, lambda b: b.get("code"))):
            r = await client.request(method, path, json=body)
            b = await r.json(content_type=None)
            check(f"inference false: {method} {path} -> 403 inference_disabled", r.status == 403 and code_of(b) == "inference_disabled",
                  f"{r.status} {b}")
        check("inference false: no node call except the single /rerank", len(n.tunnel.calls) == 1, str(n.tunnel.calls))
        st = state.CLIENT_STATS["clients"].get("memory-service") or {}
        check("inference false: Rejections in Client statistics (forbidden)", st.get("forbidden", 0) >= 7, str(st.get("forbidden")))
    finally:
        await client.close()


asyncio.run(scenarios())
print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
