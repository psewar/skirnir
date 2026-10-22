"""Observation mode (0.6.7, decision_engine.observe_blocked): when a request waits for a busy model, the Decision Engine classifies it in the background and the decision log records whether a deeper free tier of the same role would have sufficed (event `decision_observe`). Routing remains unchanged.

Background (kay, 2026-10-02): with max_parallel 1 (qwen3.8), requests queue behind minute-long agent requests. A short everyday query could often be answered by a free smaller model, but a programming task could not (benchmark: qwen3.8 19/20, others significantly lower). Before the router acts on this, a week of data should show how often this occurs, which classes are affected, how much wait time it saves, and whether the classifications are accurate.

Only the engine's CPU chain (embed/tfidf/rules, 10-19 ms on the router host): the small LLM would run on the exact GPU being waited for. Runs as a separate task; the request does not wait for it.

Additionally (decision_engine.observe_all): every request is classified and gets a line in requests.jsonl - see section "Logging for all requests" below.
"""

import asyncio
import json
import os
import time

from . import admission, decision, scheduler, state
from .common import log

LIGHT_DEFAULT = ("standard", "assist")   # Classes for which a deeper tier might be an option (not Programming, not Analysis)
CLASSIFY_PATHS = ("/api/chat", "/api/generate")
FILE_MAX_BYTES = 20 * 2 ** 20            # requests.jsonl: this large, then rotate to .1 (~400 B per line, ~50,000 requests)
LATE_WAIT_S = 5.0                        # Classification not finished when the request ends: wait for it
_warned = [0.0]


def enabled():
    return bool((state.CFG.decision or {}).get("observe_blocked")) and decision.active() is not None


def start(body, path, role, tiers, req, node, model, reason, client):
    """From _Acquire on the first wait of a request for a role: trigger background observation."""
    if not enabled():
        return
    state.spawn(_observe(body, path, role, tiers, req, node, model, reason, client))


def alternative(tiers, blocked_model, req, client_ctx=None, now=None):
    """First free local tier of the role with a different model (warm before cold, other nodes before the blocked one) -
    what a class-based fallback would take. None = none. Read-only view (mutate=False)."""
    now = now or time.time()
    found = []
    for i, t in enumerate(tiers):
        if t.get("cloud") or t["model"] == blocked_model or (req is not None and req.tier_blocked(t)):
            continue
        _ctx, _need, cands = scheduler.candidates_for(t, client_ctx, now, mutate=False)
        for n in cands:
            if not admission.saturated(n, t["model"]):
                found.append((not n.is_loaded(t["model"]), i, n.name, t["model"]))
    if not found:
        return None
    cold, i, node, model = min(found)
    return {"tier": i, "model": model, "node": node, "warm": not cold}


# --- Logging for all requests (decision_engine.observe_all) ---------------------------------------------------
# Every request to /api/chat or /api/generate is classified in the background at start (CPU chain, request does not wait),
# at the end a line is written to requests.jsonl: class, duration, tokens, thinking, tools, client, role, result - no
# prompt content. Separate file instead of decision log: ~1700 requests per day would otherwise halve its 2000-entry window.
# Purpose: check if the class predicts runtime (Retry-After, wait time estimate), and if clients choose suitable roles
# - measure first, then act.

def all_enabled():
    return bool((state.CFG.decision or {}).get("observe_all")) and decision.active() is not None


FILE_NAME, FILE_NAME_BEFORE_0_9 = "requests.jsonl", "anfragen.jsonl"


def file_path():
    p = (state.CFG.decision or {}).get("observe_path")
    if p:
        return p
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), FILE_NAME) if getattr(state.CFG, "path", None) else None


def adopt_file_before_0_9():
    """At startup: adopt the existing anfragen.jsonl (and its rotation .1) as requests.jsonl if none exists
    yet (Router 0.9.0, design/english-names.md). A custom observe_path remains as is."""
    p = file_path()
    if not p or (state.CFG.decision or {}).get("observe_path"):
        return
    old_path = os.path.join(os.path.dirname(p), FILE_NAME_BEFORE_0_9)
    for suffix in ("", ".1"):
        if os.path.exists(old_path + suffix) and not os.path.exists(p + suffix):
            try:
                os.replace(old_path + suffix, p + suffix)
            except OSError as e:  # exception-ok: file system boundary; the old file stays and a new one starts
                log.warning("%s not taken over: %s", old_path + suffix, e)
                continue
            log.info("%s -> %s (Router 0.9.0)", old_path + suffix, p + suffix)


def begin(path, body):
    """At request start: trigger classification as a separate task. None = do not log."""
    if path not in CLASSIFY_PATHS or not all_enabled():
        return None
    return state.spawn(decision.active().classify_body(body, path))


def finish(task, rec):
    """At request end: write line - immediately if classification is done, otherwise as soon as it is (max
    LATE_WAIT_S), so the response to the client never waits for the engine."""
    if task is None:
        return
    if task.done():
        _write(rec, task)
    else:
        state.spawn(_late(task, rec))


async def _late(task, rec):
    await asyncio.wait({task}, timeout=LATE_WAIT_S)
    _write(rec, task)


def _write(rec, task):
    res = None
    if task.done() and not task.cancelled() and task.exception() is None:
        res = task.result()
    if res is not None:
        rec.update({"class": res.selected, "class_engine": res.engine, "class_top": res.uncertainty.get("top"),
                    "class_margin": res.uncertainty.get("margin"),
                    "class_sure": not res.uncertainty.get("reasons") and not res.uncertainty.get("forced")})
    else:
        rec["class"] = None   # Engine hangs or error: write line anyway, without class
    p = file_path()
    if not p:
        return
    try:
        if os.path.exists(p) and os.path.getsize(p) >= FILE_MAX_BYTES:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # Disk full, permissions: not safely checkable in advance; report throttled, the request is long answered
    except OSError as e:  # exception-ok: file system boundary
        if time.time() - _warned[0] > 600:
            _warned[0] = time.time()
            log.warning("requests.jsonl write (%s): %s", p, e)


async def _observe(body, path, role, tiers, req, node, model, reason, client):
    d = decision.active()
    if d is None:
        return
    result = await d.classify_body(body, path)
    light = set((state.CFG.decision or {}).get("observe_light") or LIGHT_DEFAULT)
    confident = not result.uncertainty.get("reasons") and not result.uncertainty.get("forced")
    alt = alternative(tiers, model, req)
    state.remember({"event": "decision_observe", "request_id": req.request_id, "client": client, "role": role["name"],
                    "priority": req.priority, "blocked_model": model, "blocked_node": node.name, "wait_reason": reason,
                    "class": result.selected, "engine": result.engine, "top": result.uncertainty.get("top"),
                    "margin": result.uncertainty.get("margin"), "confident": confident, "latency_ms": round(result.latency_ms, 1),
                    "alternative": alt, "would_fallback": bool(alt and confident and result.selected in light)})
