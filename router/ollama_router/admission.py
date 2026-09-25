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


def saturated(node):
    return node.inflight >= node.effective_max_inflight()


def rank_key(w, now):
    return (PRIORITIES[w["prio"]] - (now - w["t_enq"]) / state.CFG.admission["aging_s"], w["t_enq"])


async def wait_for_slot(node, prio, t_enq, deadline_at, request_id, role):
    """Bis zu RECHECK_S (oder Deadline) auf einen Platz auf `node` warten. True = geweckt (Platz frei geworden),
    False = Zeit abgelaufen (Aufrufer prueft Deadline und waehlt ggf. neu)."""
    if len(WAITING) >= state.CFG.admission["max_queue"]:
        raise QueueFull(f"queue full ({len(WAITING)} waiting)")
    fut = asyncio.get_running_loop().create_future()
    w = {"node": node, "prio": prio, "t_enq": t_enq, "fut": fut, "request_id": request_id, "role": role}
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
    """Ein Platz auf `node` ist frei geworden: den bestplatzierten Wartenden dieses Knotens wecken."""
    now = time.time()
    cands = [w for w in WAITING if w["node"] is node and not w["fut"].done()]
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
            "entries": [{"node": w["node"].name, "priority": w["prio"], "role": w["role"], "request_id": w["request_id"],
                         "waiting_s": round(now - w["t_enq"], 1)} for w in sorted(WAITING, key=lambda w: rank_key(w, now))][:20]}
