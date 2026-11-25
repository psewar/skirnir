"""Unload a node's models and answer once their main memory is released (Router 0.44.0, ADR-0149).

The model test bench unloads a node before it starts a large job there (a VM). /api/ps is empty at once, but Ollama's
runner process keeps the model's main memory for seconds until it has exited; a job started in that gap pushed the node
to 93 % RAM. Since agent 0.37.0 the heartbeat says how many runner processes live (`ollama_runners`, ADR-0148).
`POST /admin/nodes/{fp}/unload` does the whole job, including the check, in one call:

- Before acting it refuses (409, with the facts and the next step): the node is offline, its upstream is no Ollama, its
  agent does not report runner processes, an unload already runs on it, or a measurement or benchmark runs on it (it
  would unload their model). A body that is not a JSON object is refused with 400.
- It unloads every model /api/ps lists (keep_alive 0) and waits until /api/ps is empty,
- then until a heartbeat received after that reports no runner process: 200 with `released: true`.

Otherwise it answers 503 with `released: false` and a fixed `reason`: `timeout` (a runner still lives within
`timeout_s`, for example because a request loaded a model again), `not_reported` (the agent stopped reporting runners,
for example because Ollama restarted) or `offline`. The answer is the state at that moment: requests may load models
again afterwards, and quiet (ADR-0061) stops the router's own prewarming, not requests.
"""
import asyncio
import math
import time

from aiohttp import web

from . import perf, state
from .common import log, read_json
from .outcome import failed, invalid
from .quiet import problem_response, target

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
WAIT_STEP_S = 0.5
# Nodes with an unload in progress: one at a time per node, so held-open requests cannot multiply the /api/ps polling
# through the node's tunnel. In memory only.
RUNNING = set()

# Values of the answer's `reason`
RELEASED = "released"
TIMEOUT = "timeout"
NOT_REPORTED = "not_reported"
OFFLINE = "offline"

NEXT = {
    TIMEOUT: "see loaded and ollama_runners: wait for running requests or a load to finish, then repeat",
    NOT_REPORTED: "the agent no longer reports runner processes (Ollama restarting or held); repeat once it does",
    OFFLINE: "wait until the node is online, then repeat",
}


def parse_timeout(body):
    """timeout_s from the request body (default DEFAULT_TIMEOUT_S), or a Problem."""
    t = body.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 1 <= t <= MAX_TIMEOUT_S:
        return invalid(f"timeout_s: seconds from 1 to {MAX_TIMEOUT_S} expected")
    return float(t)


def refusal(node, now):
    """Why this node cannot be unloaded with a release check now, as {"error", "next"}; None when it can."""
    if node.state == "offline":
        return {"error": "node is offline", "next": NEXT[OFFLINE]}
    if node.openai_upstream:
        return {"error": "the node's upstream is an OpenAI-compatible server, not Ollama",
                "next": "free it with the upstream hold order instead"}
    if node.runners_known(now) is None:
        return {"error": "the agent does not report Ollama's runner processes on this node",
                "next": "needs agent 0.37.0 or later on Windows with Ollama started by the agent"}
    if node.name in RUNNING:
        return {"error": "an unload is already running on this node", "next": "wait for its answer"}
    running = sorted(m for jobs in (state.MEASURING, state.BENCHING) for m, e in jobs.items() if e.get("node") == node.name)
    if running:
        return {"error": f"a measurement or benchmark runs on this node ({', '.join(running)})",
                "next": "wait until it has finished (GET /admin/state: measuring, benching)"}
    return None


def answer(node, reason, unloaded, started, now, loaded=None):
    """The outcome as facts. released only when a fresh heartbeat after the unload reported no runner process."""
    body = {"ok": reason == RELEASED, "node": node.name, "released": reason == RELEASED, "reason": reason,
            "unloaded": unloaded, "waited_s": round(now - started, 1), "ollama_runners": node.runners_known(now)}
    if loaded is not None:
        body["loaded"] = loaded
    if reason != RELEASED:
        body["error"] = "runner memory not released" if reason == TIMEOUT else "release cannot be confirmed"
        body["next"] = NEXT[reason]
    return body


async def unload_and_wait(node, timeout_s, clock=time.time, sleep=asyncio.sleep):
    """Unload every model, wait for an empty /api/ps, then for a later heartbeat without runner processes.
    Returns the answer body, or a Problem when Ollama could not be asked."""
    started = clock()
    deadline = started + timeout_s
    listed = await perf._ps_models(node)
    if failed(listed):
        return listed
    unloaded = sorted({m["name"] for m in listed if isinstance(m.get("name"), str) and m["name"]})
    for name in unloaded:
        done = await perf._ollama(node, "post", "/api/generate", {"model": name, "keep_alive": 0}, answer=False)
        if failed(done):
            return done
    while True:
        still = await perf._ps_models(node)
        if failed(still):
            return still
        if not still:
            break
        if clock() >= deadline:
            return answer(node, TIMEOUT, unloaded, started, clock(), loaded=sorted(str(m.get("name")) for m in still))
        await sleep(WAIT_STEP_S)
    empty_at = clock()
    while True:
        now = clock()
        if node.state == "offline":
            return answer(node, OFFLINE, unloaded, started, now)
        runners = node.runners_known(now)
        if runners is None:
            return answer(node, NOT_REPORTED, unloaded, started, now)
        if node.hb_ts > empty_at and runners["count"] == 0:   # a heartbeat built after /api/ps emptied, not an older one
            return answer(node, RELEASED, unloaded, started, now)
        if now >= deadline:
            return answer(node, TIMEOUT, unloaded, started, now, loaded=sorted(node.loaded))
        await sleep(WAIT_STEP_S)


async def handle_unload(request):
    """POST /admin/nodes/{fp}/unload [{"timeout_s": 1..600}]: unload everything on the node and answer once the agent
    reports its runner memory released (200), or with the facts why not (409 before acting, 503 after)."""
    node = target(request.match_info["fp"])
    if failed(node):
        return problem_response(node)
    # no body = defaults; a body that is not a JSON object (read_json: None for malformed JSON) is refused, not defaulted
    body = await read_json(request) if request.can_read_body else {}
    if not isinstance(body, dict):
        return problem_response(invalid("object expected"))
    timeout_s = parse_timeout(body)
    if failed(timeout_s):
        return problem_response(timeout_s)
    now = time.time()
    refused = refusal(node, now)
    if refused is not None:
        return web.json_response({**refused, "node": node.name, "state": node.state,
                                  "ollama_runners": node.runners_known(now)}, status=409)
    RUNNING.add(node.name)
    try:
        result = await unload_and_wait(node, timeout_s)
    finally:   # also when the caller disconnects and aiohttp cancels the handler
        RUNNING.discard(node.name)
    if failed(result):
        return problem_response(result)
    log.info("node %s: unload %s (%s) after %.1f s: %s", node.name, "released" if result["released"] else "not released",
             result["reason"], result["waited_s"], ", ".join(result["unloaded"]) or "nothing was loaded")
    state.remember({"event": "unload", "node": node.name, "released": result["released"], "reason": result["reason"],
                    "unloaded": result["unloaded"], "waited_s": result["waited_s"]})
    return web.json_response(result, status=200 if result["released"] else 503)
