"""POST /rerank on the API port: TEI-compatible reranking, forwarded to a node's Reranker service.

Wire format like Text Embeddings Inference (TEI): `{"query": str, "texts": [str], "return_text": bool}` -> 200 with
`[{"index": int, "score": float}, ...]`, one entry per text, sorted by score descending. The Reranker runs as a child of
the Agent behind its Tunnel route `rerank`; its health is reported by the Agent in the Heartbeat (`routes`, poll.route_health).

Deliberately simple: no WOL, no queue, no retry. A caller like Hindsight has its own
CPU Reranker as the next link in its chain; that is faster than any attempt to wake a node.
"""

import asyncio
import json
import math
import time

from aiohttp import ClientError, ClientTimeout, web

from . import auth, metrics, nodes, state
from .common import log
from .outcome import Problem, failed

ROUTE = "rerank"   # Name of the Tunnel route in the Agent and the entry in the Heartbeat field routes
MODEL_ID = "skirnir-rerank"   # GET /info: the Router does not know the node's model, it reports itself

# No GPU Reranker available -> 424 Failed Dependency, never 503 and never Retry-After. Hindsight's TEI client retries 429 and
# all 5xx three times with backoff (and honors Retry-After), it has no Circuit Breaker: every Recall would then cost
# seconds before its CPU fallback kicks in. Other 4xx are not retried, it immediately takes the next link in the chain.
UNAVAILABLE = 424
OUTER_GRACE_S = 0.25
REASONS = ("disabled_node", "node_offline", "tunnel_down", "reranker_down", "gpu_busy", "timeout", "upstream_invalid", "upstream_error")


def unusable(reason):
    return Problem(UNAVAILABLE, "no GPU reranker usable", reason)


def node_block(node, cfg, now):
    """Why this node cannot rerank right now, or "" if it can. Online means here: the Agent reports in
    (fresh Heartbeat). Whether Ollama responds is irrelevant, the Reranker is a separate service."""
    if node is None:
        return "disabled_node"      # not configured, not released or locked
    if not node.gpu_known(now):
        return "node_offline"
    if node.tunnel is None:
        return "tunnel_down"
    route = node.routes.get(ROUTE)
    if route is None or not route["up"]:
        return "reranker_down"      # also an Agent that does not know the routes field yet
    if node.state == "busy" and not cfg.busy_ok:
        return "gpu_busy"
    return ""


def choose(cfg, now):
    """First usable node from rerank.nodes, otherwise the Problem with the reason for the preferred (first) node."""
    first = ""
    for name in cfg.nodes:
        node = state.NODES.get(name)
        why = node_block(node, cfg, now)
        if not why:
            return node
        first = first or why
    return unusable(first or "disabled_node")


async def read_request(request, cfg):
    """Check body: (query, texts, return_text) or a Problem (400/413)."""
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
    """Check Reranker response: list with exactly one entry per text, index integer in range and unique,
    score a finite number. Returns the list sorted by score descending or the Problem upstream_invalid."""
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
    """A request through the Tunnel to the Agent's route: (Status, Body) or a Problem (timeout, upstream_error)."""
    try:
        # The Tunnel's deadline (ClientTimeout) applies first and cleans up its stream itself. The outer limit is only
        # the network behind it: if it expired simultaneously, it would abort Tunnel.open via Cancel and the stream would remain.
        async with asyncio.timeout(timeout_s + OUTER_GRACE_S):
            async with nodes.nreq(node, "POST", "/" + ROUTE, json={"query": query, "texts": texts},
                                  timeout=ClientTimeout(total=timeout_s)) as resp:
                return resp.status, await resp.read()
    except TimeoutError:  # exception-ok: network boundary, the tunnel deadline or the overall bound expired
        return unusable("timeout")
    except (ClientError, ConnectionError) as e:  # exception-ok: network boundary, tunnel broke or the agent answered with an error frame
        log.warning("rerank: node %s: tunnel request failed (%s)", node.name, type(e).__name__)
        return unusable("upstream_error")


def admit(request):
    """Shared check for /rerank and /info: None = admitted, otherwise the Problem (404 out, 403 without permission)."""
    if not state.CFG.rerank.enabled:
        return Problem(404, "rerank is not enabled", "disabled")
    name = request.get("client")
    if not name or not (state.CFG.client_auth["clients"].get(name) or {}).get("rerank"):
        if name:
            auth.audit("forbidden", client=name, path=request.path)
        return Problem(403, "client may not rerank" if name else "rerank requires an identified client", "forbidden")
    return None


async def rerank(request):
    """Result list for the response or a Problem."""
    cfg = state.CFG.rerank
    refused = admit(request)
    if refused is not None:
        return refused
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
    if problem.status == UNAVAILABLE:   # without Retry-After, see UNAVAILABLE
        return web.json_response({"error": {"code": "rerank_unavailable", "reason": problem.kind}}, status=UNAVAILABLE)
    return web.json_response({"error": {"code": problem.kind, "message": problem.message}}, status=problem.status)


async def handle_info(request):
    """GET /info like TEI. Hindsight's TEI client fetches it once during initialization and needs `model_id`; without
    /info the link fails initialization, and the chain takes the fallback forever (End-to-End test
    2026-10-20). Deliberately independent of node state: initialization happens only once per process, a failure is caught by
    424 on /rerank per request. Only permission and switch apply as with /rerank."""
    refused = admit(request)
    if refused is not None:
        return error_response(refused)
    return web.json_response({"model_id": MODEL_ID, "max_client_batch_size": state.CFG.rerank.max_texts})


async def handle_rerank(request):
    t0 = time.monotonic()
    result = await rerank(request)
    outcome = result.kind if failed(result) else "ok"
    metrics.observe_rerank(outcome, time.monotonic() - t0)
    if failed(result):
        log.debug("rerank: %d %s", result.status, outcome)
        return error_response(result)
    return web.json_response(result)
