"""Stage 3 (design/roadmap.md): Admission Control - Priority classes with aging, per-node queue, deadline.

A node accepts at most `max_inflight` requests concurrently (policy in register, else
`modes.admission.max_inflight_default`). If the chosen node is full, the request waits here instead of in Ollama's
own queue - there is no priority there. When a slot frees up, the waiting request with the best
rank is woken: class (interactive < normal < batch) minus age/aging_s, so a batch job doesn't starve behind
voice commands. If no slot is available by the deadline (routing.deadline_ms, else max_wait_s), it receives
503 with plain text. The waiter rechecks every 2 s (node may be offline/busy or another may be better).
"""

import asyncio
import time

from . import state
from .common import PRIORITIES as _PRIORITY_ORDER, log
from .outcome import unavailable

PRIORITIES = {p: i for i, p in enumerate(_PRIORITY_ORDER)}   # Class -> Rank (common.PRIORITIES is the order)
WAITING = []   # [{node, prio, t_enq, fut, request_id, role}]
RECHECK_S = 2.0


def saturated(node, model=None):
    """Node full (max_inflight) - or the model on it: catalog `max_parallel`. Cause 2026-10-02: qwen3.8 (architecture
    qwen35) cannot handle two concurrent requests in Ollama; if the router sent a second, Ollama queued it in its own
    queue and RELOADED THE RUNNER once the first finished (twice at 03:02, 5-7 s each, checkpoints and prompt cache lost).
    With this limit, the second request waits here, with priority, and the runner stays."""
    if node.inflight >= node.effective_max_inflight():
        return True
    limit = state.CFG.max_parallel(model) if model else None
    return bool(limit) and node.inflight_models[model] >= limit


def max_wait_s(prio):
    """Wait budget per priority class if the client doesn't set deadline_ms: a voice command waiting a minute
    is lost, a batch job may wait half an hour."""
    ad = state.CFG.admission
    return {"interactive": ad["max_wait_interactive_s"], "batch": ad["max_wait_batch_s"]}.get(prio, ad["max_wait_s"])


def rank_key(w, now):
    return (PRIORITIES[w["prio"]] - (now - w["t_enq"]) / state.CFG.admission["aging_s"], w["t_enq"])


async def wait_for_slot(node, prio, t_enq, deadline_at, request_id, role, model=None):
    """Wait up to RECHECK_S (or deadline) for a slot on `node` (for `model`). True = woken (slot
    freed), False = timeout (caller checks deadline and may reselect), Problem 503 = queue full."""
    if len(WAITING) >= state.CFG.admission["max_queue"]:
        return unavailable(f"queue full ({len(WAITING)} waiting)", "queue_full")
    fut = asyncio.get_running_loop().create_future()
    w = {"node": node, "prio": prio, "t_enq": t_enq, "fut": fut, "request_id": request_id, "role": role, "model": model}
    WAITING.append(w)
    try:
        # asyncio.wait reports the result (done or not) - no TimeoutError as signal for "time up"
        done, _ = await asyncio.wait({fut}, timeout=max(0.05, min(RECHECK_S, deadline_at - time.time())))
        return bool(done)
    finally:
        if w in WAITING:
            WAITING.remove(w)
        if not fut.done():
            fut.cancel()   # no one is waiting for it anymore; release() skips completed ones


def release(node):
    """A slot on `node` has freed up: wake the highest-ranked waiter for this node, who can now
    proceed (if waiting for a model still at its max_parallel limit, it stays)."""
    now = time.time()
    cands = [w for w in WAITING if w["node"] is node and not w["fut"].done() and not saturated(node, w.get("model"))]
    if not cands:
        return
    best = min(cands, key=lambda w: rank_key(w, now))
    best["fut"].set_result(True)
    log.info("admission: slot free on %s -> %s (%s, waited %.1fs, %d more)", node.name, best["request_id"], best["prio"],
             now - best["t_enq"], len(cands) - 1)


def view():
    now = time.time()
    by_node = {}
    for w in WAITING:
        by_node[w["node"].name] = by_node.get(w["node"].name, 0) + 1
    return {"waiting": len(WAITING), "by_node": by_node,
            "entries": [{"node": w["node"].name, "priority": w["prio"], "role": w["role"], "model": w.get("model"),
                         "request_id": w["request_id"], "waiting_s": round(now - w["t_enq"], 1)} for w in sorted(WAITING, key=lambda w: rank_key(w, now))][:20]}
