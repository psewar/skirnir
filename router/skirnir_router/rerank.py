"""POST /rerank auf dem API-Port: TEI-kompatibles Reranking, weitergereicht an den Reranker-Dienst eines Knotens.

Draht wie Text Embeddings Inference (TEI): `{"query": str, "texts": [str], "return_text": bool}` -> 200 mit
`[{"index": int, "score": float}, ...]`, ein Eintrag je Text, absteigend nach Score. Der Reranker laeuft als Kind des
Agenten hinter dessen Tunnel-Route `rerank`; ob er gesund ist, meldet der Agent im Heartbeat (`routes`, poll.route_health).

Bewusst schlicht: kein WOL, keine Warteschlange, keine Wiederholung. Ein Aufrufer wie Hindsight hat einen eigenen
CPU-Reranker als naechstes Glied seiner Kette; der ist schneller da als jeder Versuch, einen Knoten zu wecken.
"""

import asyncio
import json
import math
import time

from aiohttp import ClientError, ClientTimeout, web

from . import auth, metrics, nodes, state
from .common import log
from .outcome import Problem, failed

ROUTE = "rerank"   # Name der Tunnel-Route im Agenten und des Eintrags im Heartbeat-Feld routes

# Kein GPU-Reranker nutzbar -> 424 Failed Dependency, nie 503 und nie Retry-After. Hindsights TEI-Client wiederholt 429 und
# alle 5xx dreimal mit Backoff (und beachtet Retry-After), einen Circuit Breaker hat er nicht: jeder Recall kostete dann
# Sekunden, bevor sein CPU-Fallback greift. Andere 4xx wiederholt er nicht, sondern nimmt sofort das naechste Glied der Kette.
UNAVAILABLE = 424
OUTER_GRACE_S = 0.25
REASONS = ("disabled_node", "node_offline", "tunnel_down", "reranker_down", "gpu_busy", "timeout", "upstream_invalid", "upstream_error")


def unusable(reason):
    return Problem(UNAVAILABLE, "no GPU reranker usable", reason)


def node_block(node, cfg, now):
    """Warum dieser Knoten jetzt nicht reranken kann, oder "" wenn er es kann. Online heisst hier: der Agent meldet sich
    (frischer Heartbeat). Ob Ollama antwortet, spielt keine Rolle, der Reranker ist ein eigener Dienst."""
    if node is None:
        return "disabled_node"      # nicht konfiguriert, nicht freigegeben oder gesperrt
    if not node.gpu_known(now):
        return "node_offline"
    if node.tunnel is None:
        return "tunnel_down"
    route = node.routes.get(ROUTE)
    if route is None or not route["up"]:
        return "reranker_down"      # auch ein Agent, der das Feld routes noch nicht kennt
    if node.state == "busy" and not cfg.busy_ok:
        return "gpu_busy"
    return ""


def choose(cfg, now):
    """Erster nutzbarer Knoten aus rerank.nodes, sonst das Problem mit dem Grund des bevorzugten (ersten) Knotens."""
    first = ""
    for name in cfg.nodes:
        node = state.NODES.get(name)
        why = node_block(node, cfg, now)
        if not why:
            return node
        first = first or why
    return unusable(first or "disabled_node")


async def read_request(request, cfg):
    """Body pruefen: (query, texts, return_text) oder ein Problem (400/413)."""
    if request.content_length is not None and request.content_length > cfg.max_body_bytes:
        return Problem(413, f"body larger than {cfg.max_body_bytes} bytes", "too_large")
    raw = await request.read()
    if len(raw) > cfg.max_body_bytes:
        return Problem(413, f"body larger than {cfg.max_body_bytes} bytes", "too_large")
    try:
        body = json.loads(raw)
    except ValueError:  # exception-ok: parsing untrusted JSON; whether the body is JSON shows only the parse
        return Problem(400, "body must be JSON", "invalid")
    if not isinstance(body, dict):
        return Problem(400, "body must be a JSON object", "invalid")
    query, texts, return_text = body.get("query"), body.get("texts"), body.get("return_text", False)
    if not isinstance(query, str) or not query:
        return Problem(400, "query must be a nonempty string", "invalid")
    if not isinstance(texts, list) or not texts or any(not isinstance(t, str) for t in texts):
        return Problem(400, "texts must be a nonempty list of strings", "invalid")
    if len(texts) > cfg.max_texts:
        return Problem(413, f"more than {cfg.max_texts} texts", "too_large")
    if type(return_text) is not bool:
        return Problem(400, "return_text must be boolean", "invalid")
    return query, texts, return_text


def check_scores(raw, count):
    """Antwort des Rerankers pruefen: Liste mit genau einem Eintrag je Text, index ganzzahlig im Bereich und eindeutig,
    score eine endliche Zahl. Liefert die Liste absteigend nach Score oder das Problem upstream_invalid."""
    try:
        data = json.loads(raw)
    except ValueError:  # exception-ok: parsing the node's answer; whether it is JSON shows only the parse
        return unusable("upstream_invalid")
    if not isinstance(data, list) or len(data) != count:
        return unusable("upstream_invalid")
    seen, out = set(), []
    for e in data:
        if not isinstance(e, dict):
            return unusable("upstream_invalid")
        i, s = e.get("index"), e.get("score")
        if type(i) is not int or not 0 <= i < count or i in seen:
            return unusable("upstream_invalid")
        if type(s) not in (int, float) or not math.isfinite(s):
            return unusable("upstream_invalid")
        seen.add(i)
        out.append({"index": i, "score": float(s)})
    out.sort(key=lambda e: -e["score"])
    return out


async def exchange(node, query, texts, timeout_s):
    """Eine Anfrage durch den Tunnel an die Route des Agenten: (Status, Body) oder ein Problem (timeout, upstream_error)."""
    try:
        # Die Frist des Tunnels (ClientTimeout) greift zuerst und raeumt ihren Stream selbst ab. Die aeussere Grenze ist nur
        # das Netz dahinter: liefe sie gleichzeitig ab, braeche sie Tunnel.open per Cancel ab und der Stream bliebe liegen.
        async with asyncio.timeout(timeout_s + OUTER_GRACE_S):
            async with nodes.nreq(node, "POST", "/" + ROUTE, json={"query": query, "texts": texts},
                                  timeout=ClientTimeout(total=timeout_s)) as resp:
                return resp.status, await resp.read()
    except TimeoutError:  # exception-ok: network boundary, the tunnel deadline or the overall bound expired
        return unusable("timeout")
    except (ClientError, ConnectionError) as e:  # exception-ok: network boundary, tunnel broke or the agent answered with an error frame
        log.warning("rerank: node %s: tunnel request failed (%s)", node.name, type(e).__name__)
        return unusable("upstream_error")


async def rerank(request):
    """Ergebnisliste fuer die Antwort oder ein Problem."""
    cfg = state.CFG.rerank
    if not cfg.enabled:
        return Problem(404, "rerank is not enabled", "disabled")
    name = request.get("client")
    if not name or not (state.CFG.client_auth["clients"].get(name) or {}).get("rerank"):
        if name:
            auth.audit("forbidden", client=name, path=request.path)
        return Problem(403, "client may not rerank" if name else "rerank requires an identified client", "forbidden")
    parsed = await read_request(request, cfg)
    if failed(parsed):
        return parsed
    query, texts, return_text = parsed
    node = choose(cfg, time.time())
    if failed(node):
        return node
    answer = await exchange(node, query, texts, cfg.timeout_s)
    if failed(answer):
        return answer
    status, raw = answer
    if status != 200:
        log.warning("rerank: node %s answered HTTP %d", node.name, status)
        return unusable("upstream_error")
    scores = check_scores(raw, len(texts))
    if failed(scores):
        log.warning("rerank: node %s sent an invalid answer", node.name)
        return scores
    if return_text:
        for e in scores:
            e["text"] = texts[e["index"]]
    return scores


def error_response(problem):
    if problem.status == UNAVAILABLE:   # ohne Retry-After, siehe UNAVAILABLE
        return web.json_response({"error": {"code": "rerank_unavailable", "reason": problem.kind}}, status=UNAVAILABLE)
    return web.json_response({"error": {"code": problem.kind, "message": problem.message}}, status=problem.status)


async def handle_rerank(request):
    t0 = time.monotonic()
    result = await rerank(request)
    outcome = result.kind if failed(result) else "ok"
    metrics.observe_rerank(outcome, time.monotonic() - t0)
    if failed(result):
        log.debug("rerank: %d %s", result.status, outcome)
        return error_response(result)
    return web.json_response(result)
