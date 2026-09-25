"""Stufe 6 (design/roadmap.md): Betrieb - Idempotency, Canary, Shadow, Supply-Chain-Metadaten.

Idempotency: `Idempotency-Key`-Header (oder routing.idempotency_key) bei NICHT-streamenden Anfragen. Die erste Anfrage
laeuft, ihre Antwort (Status 200) wird `router.idempotency_ttl_s` lang behalten; eine Wiederholung mit demselben Schluessel
bekommt dieselbe Antwort zurueck (Header X-Skirnir-Idempotent-Replay: 1), eine gleichzeitige Wiederholung wartet auf die
laufende. Streams werden nicht gecacht (der Client bekommt sie ohnehin nur einmal in Echtzeit).

Canary: `roles.<r>.canary: {model, num_ctx, percent}` - fuer den Anteil der Anfragen rueckt die Canary-Stufe an die erste
Stelle der Stufenliste; das Ergebnis geht normal an den Client, Routing-Info und Entscheidungsprotokoll tragen `canary`.
Damit laesst sich ein Kandidatenmodell mit echtem Verkehr messen (perf, outcomes, Usage je Modell).

Shadow: `roles.<r>.shadow: {model, num_ctx, percent}` - NACH der Antwort an den Client laeuft dieselbe Anfrage noch einmal
ohne Stream gegen das Schattenmodell (nur auf einem freien, nicht vollen Knoten, nie zwei Schatten gleichzeitig). Der Client
sieht nichts davon; perf/outcomes/Usage (Client "shadow") und das Ereignis `shadow` halten fest, wie das Modell sich schlaegt.

Supply Chain: Ollama-Version je Knoten (Poll), Modell-Digests aus /api/tags, Deploy-Manifest (manifest.json: Zeitpunkt,
SHA-256 der ausgerollten Dateien, von deploy.py geschrieben) -> /admin/state `build` und `supply_chain`.
"""

import asyncio
import json
import os
import random
import time

from aiohttp import ClientTimeout, web

from . import admission, metrics, nodes, perf, scheduler, state
from .common import log

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
    """Gecachte Antwort (dict) oder None, wenn diese Anfrage die erste ist (dann ist der Schluessel jetzt reserviert)."""
    now = time.time()
    _prune(now)
    e = IDEM.get(key)
    if e is not None and e.get("fut") is not None:
        try:
            await asyncio.wait_for(asyncio.shield(e["fut"]), timeout=state.CFG.request_timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
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
        cands = [n for n in cands if not admission.saturated(n)]
        if not cands:
            state.remember({"event": "shadow", "role": role["name"], "model": s["model"], "node": None, "status": "kein Knoten",
                            "request_id": request_id})
            return
        node = scheduler.rank(cands, s["model"])[0]
        if node.is_cloud:   # Schattenlaeufe kosten in der Cloud Geld - bewusst nicht
            state.remember({"event": "shadow", "role": role["name"], "model": s["model"], "node": node.name, "status": "cloud uebersprungen", "request_id": request_id})
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
        except asyncio.TimeoutError:
            outcome = "timeout"
        except Exception as e:  # noqa: BLE001
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
    try:
        with open(manifest_path(), encoding="utf-8") as f:
            m = json.load(f)
        return {"present": True, **m}
    except FileNotFoundError:
        return {"present": False}
    except Exception as e:  # noqa: BLE001
        return {"present": False, "error": str(e)}


def supply_chain():
    out = {}
    for n in state.NODES.values():
        out[n.name] = {"ollama_version": n.ollama_version, "state": n.state,
                       "models": {m: {"digest": d.get("digest"), "size": d.get("size"), "modified_at": d.get("modified_at"),
                                      "family": (d.get("details") or {}).get("family"),
                                      "quantization": (d.get("details") or {}).get("quantization_level")}
                                  for m, d in sorted(n.model_details.items())}}
    return out
