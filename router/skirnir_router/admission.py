"""Stufe 3 (design/roadmap.md): Admission Control - Prioritaetsklassen mit Aging, Warteschlange je Knoten, Deadline.

Ein Knoten nimmt hoechstens `max_inflight` Anfragen gleichzeitig (Policy im Register, sonst
`modes.admission.max_inflight_default`). Ist der gewaehlte Knoten voll, wartet die Anfrage hier statt in Ollamas
eigener Schlange - dort gibt es keine Prioritaet. Beim Freiwerden eines Platzes wird der Wartende mit dem besten
Rang geweckt: Klasse (interactive < normal < batch) minus Alter/aging_s, damit ein Batch-Auftrag nicht endlos hinter
Sprachbefehlen verhungert. Wer bis zur Deadline (routing.deadline_ms, sonst max_wait_s) keinen Platz bekommt, erhaelt
503 mit Klartext. Der Wartende prueft alle 2 s neu (Knoten kann inzwischen offline/busy sein oder ein anderer besser).
"""

import asyncio
import time

from . import state
from .common import PRIORITIES as _PRIORITY_ORDER, log

PRIORITIES = {p: i for i, p in enumerate(_PRIORITY_ORDER)}   # Klasse -> Rang (common.PRIORITIES ist die Reihenfolge)
WAITING = []   # [{node, prio, t_enq, fut, request_id, role}]
RECHECK_S = 2.0


class QueueFull(Exception):
    pass


def saturated(node, model=None):
    """Knoten voll (max_inflight) - oder das Modell darauf: Katalog `max_parallel`. Anlass 2026-10-02: qwen3.8 (Architektur
    qwen35) kann in Ollama keine zwei Anfragen zugleich; schickte der Router eine zweite, stellte Ollama sie in seine eigene
    Schlange und LUD DEN RUNNER NEU, sobald die erste fertig war (zweimal 03:02, je 5-7 s, Checkpoints und Prompt-Cache weg).
    Mit der Grenze wartet die zweite Anfrage hier, mit Prioritaet, und der Runner bleibt."""
    if node.inflight >= node.effective_max_inflight():
        return True
    limit = state.CFG.max_parallel(model) if model else None
    return bool(limit) and node.inflight_models[model] >= limit


def max_wait_s(prio):
    """Wartebudget je Prioritaetsklasse, wenn der Client keine deadline_ms setzt: ein Sprachbefehl, der eine Minute wartet,
    ist verloren, ein Batch-Auftrag darf eine halbe Stunde warten."""
    ad = state.CFG.admission
    return {"interactive": ad["max_wait_interactive_s"], "batch": ad["max_wait_batch_s"]}.get(prio, ad["max_wait_s"])


def rank_key(w, now):
    return (PRIORITIES[w["prio"]] - (now - w["t_enq"]) / state.CFG.admission["aging_s"], w["t_enq"])


async def wait_for_slot(node, prio, t_enq, deadline_at, request_id, role, model=None):
    """Bis zu RECHECK_S (oder Deadline) auf einen Platz auf `node` (fuer `model`) warten. True = geweckt (Platz frei
    geworden), False = Zeit abgelaufen (Aufrufer prueft Deadline und waehlt ggf. neu)."""
    if len(WAITING) >= state.CFG.admission["max_queue"]:
        raise QueueFull(f"queue full ({len(WAITING)} waiting)")
    fut = asyncio.get_running_loop().create_future()
    w = {"node": node, "prio": prio, "t_enq": t_enq, "fut": fut, "request_id": request_id, "role": role, "model": model}
    WAITING.append(w)
    try:
        await asyncio.wait_for(fut, timeout=max(0.05, min(RECHECK_S, deadline_at - time.time())))
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        if w in WAITING:
            WAITING.remove(w)


def release(node):
    """Ein Platz auf `node` ist frei geworden: den bestplatzierten Wartenden dieses Knotens wecken, der jetzt auch
    loslegen kann (wartet er auf ein Modell, das noch an seiner max_parallel-Grenze ist, bleibt er liegen)."""
    now = time.time()
    cands = [w for w in WAITING if w["node"] is node and not w["fut"].done() and not saturated(node, w.get("model"))]
    if not cands:
        return
    best = min(cands, key=lambda w: rank_key(w, now))
    best["fut"].set_result(True)
    log.info("admission: Platz auf %s frei -> %s (%s, wartete %.1fs, %d weitere)", node.name, best["request_id"], best["prio"],
             now - best["t_enq"], len(cands) - 1)


def view():
    now = time.time()
    by_node = {}
    for w in WAITING:
        by_node[w["node"].name] = by_node.get(w["node"].name, 0) + 1
    return {"waiting": len(WAITING), "by_node": by_node,
            "entries": [{"node": w["node"].name, "priority": w["prio"], "role": w["role"], "model": w.get("model"),
                         "request_id": w["request_id"], "waiting_s": round(now - w["t_enq"], 1)} for w in sorted(WAITING, key=lambda w: rank_key(w, now))][:20]}
