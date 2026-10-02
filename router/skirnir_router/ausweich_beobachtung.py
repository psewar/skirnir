"""Beobachtungsmodus (0.6.7, decision_engine.observe_blocked): wartet eine Anfrage auf ein belegtes Modell, ordnet die
Decision Engine sie im Hintergrund ein und das Entscheidungsprotokoll haelt fest, ob eine tiefere freie Stufe derselben Rolle
gereicht haette (Ereignis `decision_observe`). Am Routing aendert sich nichts.

Hintergrund (kay, 2026-10-02): mit max_parallel 1 (qwen3.8) warten Anfragen im Router hinter minutenlangen Agenten-Anfragen.
Eine kurze Alltagsfrage koennte oft ein freies kleineres Modell beantworten, eine Programmieraufgabe nicht (Pruefstand: qwen3.8
19/20, die anderen deutlich darunter). Bevor der Router danach handelt, soll eine Woche zeigen, wie oft der Fall eintritt, welche
Klassen betroffen sind, wie viel Wartezeit es sparte und ob die Einordnungen stimmen.

Nur die CPU-Kette der Engine (embed/tfidf/rules, 10-19 ms auf dem Router-Host): das kleine LLM liefe auf genau der GPU, auf die
gewartet wird. Laeuft als eigene Aufgabe, die Anfrage wartet nicht darauf.

Dazu (decision_engine.observe_all): jede Anfrage wird eingeordnet und bekommt am Ende eine Zeile in anfragen.jsonl - siehe
Abschnitt "Mitschreiben fuer alle Anfragen" unten.
"""

import asyncio
import json
import os
import time

from . import admission, decision, scheduler, state
from .common import log

LIGHT_DEFAULT = ("standard", "assist")   # Klassen, fuer die eine tiefere Stufe in Frage kaeme (Programmieren, Analysen nicht)
CLASSIFY_PATHS = ("/api/chat", "/api/generate")
FILE_MAX_BYTES = 20 * 2 ** 20            # anfragen.jsonl: so gross, dann nach .1 rotieren (~400 B je Zeile, ~50 000 Anfragen)
LATE_WAIT_S = 5.0                        # Einordnung noch nicht fertig, wenn die Anfrage endet: so lange darauf warten
_warned = [0.0]


def enabled():
    return bool((state.CFG.decision or {}).get("observe_blocked")) and decision.active() is not None


def start(body, path, role, tiers, req, node, model, reason, client):
    """Aus _Acquire beim ersten Warten einer Anfrage auf eine Rolle: Beobachtung im Hintergrund anstossen."""
    if not enabled():
        return
    state.spawn(_observe(body, path, role, tiers, req, node, model, reason, client))


def alternative(tiers, blocked_model, req, client_ctx=None, now=None):
    """Erste freie lokale Stufe der Rolle mit einem anderen Modell (warm vor kalt, andere Knoten vor dem blockierten) -
    was ein Ausweichen nach Klasse nehmen wuerde. None = keine. Reine Sicht (mutate=False)."""
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


# --- Mitschreiben fuer alle Anfragen (decision_engine.observe_all) ---------------------------------------------------
# Jede Anfrage auf /api/chat oder /api/generate wird beim Start im Hintergrund eingeordnet (CPU-Kette, die Anfrage wartet nicht),
# am Ende steht eine Zeile in anfragen.jsonl: Klasse, Dauer, Token, Denken, Werkzeuge, Client, Rolle, Ergebnis - keine
# Prompt-Inhalte. Eigene Datei statt Entscheidungsprotokoll: ~1700 Anfragen am Tag halbierten sonst dessen 2000er-Fenster.
# Wofuer: pruefen, ob die Klasse die Laufzeit vorhersagt (Retry-After, Wartezeit-Schaetzung), und ob Clients passende Rollen
# waehlen - erst messen, dann danach handeln.

def all_enabled():
    return bool((state.CFG.decision or {}).get("observe_all")) and decision.active() is not None


def file_path():
    p = (state.CFG.decision or {}).get("observe_path")
    if p:
        return p
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "anfragen.jsonl") if getattr(state.CFG, "path", None) else None


def begin(path, body):
    """Beim Start einer Anfrage: Einordnung als eigene Aufgabe anstossen. None = nicht mitschreiben."""
    if path not in CLASSIFY_PATHS or not all_enabled():
        return None
    return state.spawn(decision.active().classify_body(body, path))


def finish(task, rec):
    """Am Ende einer Anfrage: Zeile schreiben - sofort, wenn die Einordnung fertig ist, sonst sobald sie es ist (hoechstens
    LATE_WAIT_S), damit die Antwort an den Client nie auf die Engine wartet."""
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
        rec["class"] = None   # Engine haengt oder Fehler: Zeile trotzdem, ohne Klasse
    p = file_path()
    if not p:
        return
    try:
        if os.path.exists(p) and os.path.getsize(p) >= FILE_MAX_BYTES:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:   # Platte voll, Rechte: nicht vorab sicher pruefbar; gedrosselt melden, die Anfrage ist laengst beantwortet
        if time.time() - _warned[0] > 600:
            _warned[0] = time.time()
            log.warning("anfragen.jsonl schreiben (%s): %s", p, e)


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
