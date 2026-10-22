"""Stage 6 (design/roadmap.md): Operations - Idempotency, Canary, Shadow, Supply-Chain metadata.

Idempotency: `Idempotency-Key` header (or routing.idempotency_key) for non-streaming requests. The first request
executes, and its response (Status 200) is retained for `router.idempotency_ttl_s`; a repeat with the same key
receives the same response (Header X-Skirnir-Idempotent-Replay: 1), while a concurrent repeat waits for the
running one. Streams are not cached (the client receives them only once in real-time anyway).

Canary: `roles.<r>.canary: {model, num_ctx, percent}` - for the specified percentage of requests, the canary tier is moved to the
first position in the tier list; the result goes normally to the client, and routing info and decision logs carry `canary`.
This allows measuring a candidate model with real traffic (perf, outcomes, Usage per model).

Shadow: `roles.<r>.shadow: {model, num_ctx, percent}` - AFTER the response to the client, the same request runs again
without streaming against the shadow model (only on a free, not full node, never two shadows at once). The client
sees nothing of this; perf/outcomes/Usage (Client "shadow") and the `shadow` event record how the model performs.

Supply Chain: Ollama version per node (Poll), model digests from /api/tags, Deploy manifest (manifest.json: timestamp,
SHA-256 of deployed files, written by deploy.py) -> /admin/state `build` and `supply_chain`.
"""

import asyncio
import json
import os
import random
import time

from aiohttp import ClientError, ClientTimeout, web

from . import admission, metrics, nodes, perf, scheduler, state
from .common import load_json_file, log

IDEM = {}            # key -> {"t", "fut", "status", "body", "content_type", "headers"}
IDEM_MAX = 500
IDEM_MAX_BODY = 1024 * 1024
SHADOW_BUSY = [False]


# ---------------- Idempotency ----------------

def idem_key(request, body, path):
    if body.get("stream", True) and path in ("/api/chat", "/api/generate"):
        return None
    key = request.headers.get("Idempotency-Key")
    rb = body.get("routing")
    if not key and isinstance(rb, dict) and rb.get("idempotency_key"):
        key = str(rb["idempotency_key"])
    if key:
        key = key.strip()[:200]
    return key or None


def _prune(now):
    ttl = state.CFG.idempotency_ttl_s
    for k in [k for k, e in IDEM.items() if e.get("fut") is None and now - e["t"] > ttl]:
        IDEM.pop(k, None)
    if len(IDEM) > IDEM_MAX:
        for k in sorted(IDEM, key=lambda k: IDEM[k]["t"])[: len(IDEM) - IDEM_MAX]:
            if IDEM[k].get("fut") is None:
                IDEM.pop(k, None)


async def idem_wait(key):
    """Cached response (dict) or None if this request is the first (then the key is now reserved)."""
    now = time.time()
    _prune(now)
    e = IDEM.get(key)
    if e is not None and e.get("fut") is not None:
        # asyncio.wait does not abort the running initial request and reports the deadline as a value; if THIS request is aborted,
        # it propagates upwards as everywhere else (until 0.8.10 an except swallowed it and continued as an initial request)
        done, _ = await asyncio.wait({e["fut"]}, timeout=state.CFG.request_timeout_s)
        if not done:
            return None
        e = IDEM.get(key)
    if e is not None and e.get("body") is not None and now - e["t"] <= state.CFG.idempotency_ttl_s:
        return e
    IDEM[key] = {"t": now, "fut": asyncio.get_running_loop().create_future(), "body": None}
    return None


def idem_finish(key, resp):
    e = IDEM.get(key)
    if e is None:
        return
    fut = e.pop("fut", None)
    ok = isinstance(resp, web.Response) and resp.status == 200 and resp.body is not None and len(resp.body) <= IDEM_MAX_BODY
    if ok:
        e.update({"t": time.time(), "status": resp.status, "body": bytes(resp.body), "content_type": resp.content_type,
                  "headers": {k: v for k, v in resp.headers.items() if k.startswith("X-Skirnir")}})
    else:
        IDEM.pop(key, None)
    if fut is not None and not fut.done():
        fut.set_result(ok)


def idem_abort(key):
    e = IDEM.pop(key, None)
    if e and e.get("fut") is not None and not e["fut"].done():
        e["fut"].set_result(False)


def idem_replay(e):
    h = dict(e.get("headers") or {})
    h["X-Skirnir-Idempotent-Replay"] = "1"
    return web.Response(status=e["status"], body=e["body"], content_type=e.get("content_type") or "application/json", headers=h)


def idem_view():
    return {"entries": len(IDEM), "ttl_s": state.CFG.idempotency_ttl_s}


# ---------------- Canary / Shadow ----------------

def canary_tier(role):
    c = role.get("canary")
    if not c or c["percent"] <= 0:
        return None
    if random.random() * 100.0 >= c["percent"]:
        return None
    return {"model": c["model"], "num_ctx": c["num_ctx"], "busy_ok": c.get("busy_ok", False), "canary": True}


def maybe_shadow(role, body, primary_node, req):
    s = role.get("shadow")
    if not s or s["percent"] <= 0 or random.random() * 100.0 >= s["percent"]:
        return
    if SHADOW_BUSY[0]:
        return
    state.spawn(run_shadow(role, s, dict(body), primary_node, req.request_id))


async def run_shadow(role, s, body, primary_node, request_id):
    SHADOW_BUSY[0] = True
    node = None
    t0 = time.time()
    try:
        tier = {"model": s["model"], "num_ctx": s["num_ctx"], "busy_ok": s.get("busy_ok", False)}
        ctx, need, cands = scheduler.candidates_for(tier, None, t0)
        cands = [n for n in cands if not admission.saturated(n, s["model"])]
        if not cands:
            state.remember({"event": "shadow", "role": role["name"], "model": s["model"], "node": None, "status": "no_node",
                            "request_id": request_id})
            return
        node = scheduler.rank(cands, s["model"])[0]
        if node.is_cloud:   # Shadow runs cost money in the cloud - intentionally not
            state.remember({"event": "shadow", "role": role["name"], "model": s["model"], "node": node.name, "status": "cloud_skipped", "request_id": request_id})
            return
        out = dict(body)
        out.pop("routing", None)
        out["model"] = s["model"]
        out["stream"] = False
        out["options"] = {**(out.get("options") or {}), "num_ctx": ctx}
        out["keep_alive"] = state.CFG.keep_alive.get(node.state, "5m")
        path = "/api/generate" if "prompt" in out and "messages" not in out else "/api/chat"
        node.inflight += 1
        node.inflight_models[s["model"]] += 1
        warm = node.is_loaded(s["model"])
        if not warm:
            node.announce_load(s["model"], ctx)
        outcome, ptoks, ctoks = "error", 0, 0
        try:
            async with nodes.nreq(node, "post", path, json=out, timeout=ClientTimeout(total=state.CFG.request_timeout_s)) as r:
                data = await r.read()
                if r.status == 200:
                    j = json.loads(data)
                    if isinstance(j, dict) and j.get("eval_count"):
                        perf.perf_record(s["model"], node.name, j, None, kind="shadow")
                        ptoks, ctoks = int(j.get("prompt_eval_count") or 0), int(j.get("eval_count") or 0)
                    outcome = "ok"
                else:
                    outcome = "structured_error" if out.get("format") and r.status >= 500 else "error"
        except asyncio.TimeoutError:  # exception-ok: network boundary; the shadow run ran out of time and counts as timeout
            outcome = "timeout"
        except (ClientError, OSError, ValueError) as e:  # exception-ok: network boundary and the node's JSON; only counted
            log.info("shadow %s on %s: %s", s["model"], node.name, e)
        finally:
            node.inflight -= 1
            node.inflight_models[s["model"]] -= 1
            node.finish_load(s["model"])
            admission.release(node)
        perf.perf_outcome(s["model"], node.name, outcome)
        info = {"role": role["name"], "node": node.name, "model": s["model"], "client": "shadow", "via": "shadow", "priority": "batch"}
        metrics.observe_request(info, outcome, time.time() - t0, None, ptoks, ctoks, 0)
        state.remember({"event": "shadow", "role": role["name"], "model": s["model"], "node": node.name, "status": outcome,
                        "warm": warm, "ms": round((time.time() - t0) * 1000), "request_id": request_id})
        log.info("shadow %s -> %s on %s: %s %.1fs", role["name"], s["model"], node.name, outcome, time.time() - t0)
    finally:
        SHADOW_BUSY[0] = False


# ---------------- Supply Chain ----------------

def manifest_path():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "manifest.json")


def build_info():
    if not os.path.exists(manifest_path()):
        return {"present": False}
    m = load_json_file(manifest_path(), "manifest.json")
    return {"present": True, **m} if isinstance(m, dict) else {"present": False, "error": "manifest.json unreadable"}


def supply_chain():
    out = {}
    for n in state.NODES.values():
        out[n.name] = {"ollama_version": n.ollama_version, "state": n.state,
                       "models": {m: {"digest": d.get("digest"), "size": d.get("size"), "modified_at": d.get("modified_at"),
                                      "family": (d.get("details") or {}).get("family"),
                                      "quantization": (d.get("details") or {}).get("quantization_level")}
                                  for m, d in sorted(n.model_details.items())}}
    return out
