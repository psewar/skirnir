"""Auswahl: Rolle -> Tiers -> passende Knoten -> Rang."""

import time

from . import cloud, perf
from . import request as request_mod
from . import state


def known_concrete(name):
    """Konkreter Modellname, den irgendein Knoten hält (jetzt oder zuletzt gesehen). Liefert den Namen mit Tag."""
    for cand in (name, f"{name}:latest"):
        if any(cand in n.models or cand in n.last_known_models for n in state.NODES.values()):
            return cand
    return None


def resolve_tiers(name, body):
    if name.startswith("workload:"):   # Alias aus der Spezifikation (§1): workload:standard == standard:latest
        base = name[len("workload:"):]
        name = next((c for c in (base, f"{base}:latest") if c in state.CFG.roles), name)
    if name in state.CFG.roles:
        return state.CFG.roles[name], state.CFG.roles[name]["tiers"]
    if state.CFG.expose_concrete:
        concrete = known_concrete(name)
        if concrete:
            ctx = int((body.get("options") or {}).get("num_ctx") or 8192)
            return ({"name": concrete, "exposed": concrete, "latency_first": True},
                    [{"model": concrete, "num_ctx": ctx, "busy_ok": concrete in state.CFG.busy_ok_models}])
    return None, None


def candidates_for(tier, client_ctx, now, exclude=()):
    ctx = min(client_ctx, tier["num_ctx"]) if client_ctx else tier["num_ctx"]
    need = None
    out = []
    if tier.get("cloud"):   # Stufe 5: der Anbieter ist der einzige "Knoten" dieser Stufe (Schranken prueft request.tier_blocked)
        t = cloud.target_for(tier)
        if t is not None and t.name not in exclude and t.breaker_allows(now):
            out.append(t)
        return ctx, 0.0, out
    for n in state.NODES.values():
        if n.name in exclude or n.state == "offline" or tier["model"] not in n.models:
            continue
        if n.state == "busy" and not tier["busy_ok"]:
            continue
        if not n.breaker_allows(now):   # Stufe 3: Knoten mit offenem Breaker bleiben aussen vor (half_open: eine Probe)
            continue
        need = state.CFG.need_gib(tier["model"], ctx, n)
        lc = n.loaded_context(tier["model"])
        if n.is_loaded(tier["model"]) and (lc is None or lc >= ctx):
            # Schon geladen mit ausreichendem Kontext: braucht kein weiteres VRAM, also keine Budgetpruefung.
            # Der Heartbeat waehrend/kurz nach einer Antwort zeigt wenig freies VRAM (Rechenpuffer, KV); mit der
            # Budgetpruefung fiel am 2026-09-10 der warme Hauptknoten 0,2 s nach seiner eigenen Antwort durch,
            # und die Folgeanfrage lief auf der Ausweichstufe des anderen Knotens.
            out.append(n)
            continue
        if need > n.budget_gib(now, tier["model"]):
            continue
        out.append(n)
    return ctx, need, out


def score(node, model):
    """Stufe 3: gewichtbarer Score (modes.score). Die Vorgaben bilden die alte Reihenfolge ab - warm (100) vor wenig
    laufenden Anfragen (10 je Anfrage; voller Knoten zusaetzlich -50) vor freiem VRAM (Anteil x2) vor Gewicht (x1) -
    und ergaenzen sie um gemessenes Tempo (EWMA tok/s / 100 x2), juengste Fehlerrate (x20) und Breaker-Probe (-5)."""
    w = state.CFG.score
    s = w["warm"] if node.is_loaded(model) else 0.0
    s -= w["inflight"] * node.inflight
    if node.inflight >= node.effective_max_inflight():
        s -= w["saturated"]
    gg = state.CFG.gpu_guard   # GPU-Schutz: Knoten in Hochlast/Stufe 2 weichen einem zweiten Knoten, der die Stufe tragen kann
    if gg["enabled"] and getattr(node, "guard_policy", False) and node.guard_state() in ("hochlast", "gedrosselt"):   # CloudTarget hat keinen Guard
        s -= gg["score_penalty"]
    if node.vram_total_gib and node.vram_free_gib is not None:
        s += w["vram_free"] * (node.vram_free_gib / node.vram_total_gib)
    s += w["weight"] * node.weight
    tps = perf.ewma_gen_tps(model, node.name)
    if tps:
        s += w["speed"] * tps / 100.0
    s -= w["errors"] * perf.error_rate(model, node.name)
    if node.breaker == "half_open":
        s -= w["half_open"]
    return round(s, 3)


def rank(nodes, model, req=None):
    scored = sorted(((score(n, model), n) for n in nodes), key=lambda x: (-x[0], x[1].name))
    if req is not None:
        req.candidates = [{"node": n.name, "score": s, "warm": n.is_loaded(model), "inflight": n.inflight, "state": n.state}
                          for s, n in scored]
    return [n for s, n in scored]


def choose(role, tiers, client_ctx, now, exclude=(), req=None):
    """Liefert (tier_index, tier, ctx, node) oder None. `req` (request.Routing) filtert Stufen nach Anforderungen,
    bevorzugt Stufen mit gewuenschten Faehigkeiten und haelt eine Session auf ihrem warmen Knoten."""
    per_tier = []
    if req is not None:
        req.skipped = []
    for i, t in enumerate(tiers):
        if req is not None:
            why = req.tier_blocked(t)
            if why:
                req.skipped.append({"tier": i, "model": t["model"], "ctx": t["num_ctx"], "reason": why})
                continue
        ctx, need, cands = candidates_for(t, client_ctx, now, exclude)
        per_tier.append((i, t, ctx, need, cands))
    if req is not None and req.canary:
        # Stufe 6: ausgeloste Canary-Stufe bekommt den Verkehr auch kalt - sonst saehe ein Kandidatenmodell neben einem
        # warmen Hauptmodell nie eine Anfrage. Kann kein Knoten sie bedienen, geht es normal weiter.
        for i, t, ctx, need, cands in per_tier:
            if t.get("canary") and cands:
                req.reason = "canary"
                return i, t, ctx, rank(cands, t["model"], req)[0]
    if req is not None and req.prefer:
        pref = [pt for pt in per_tier if not request_mod.lacks(pt[1]["model"], sorted(req.prefer))]
        if any(pt[4] for pt in pref):
            per_tier = pref
    if req is not None and req.session_id:
        aff = request_mod.affinity(req.session_id)
        if aff:
            for i, t, ctx, need, cands in per_tier:
                if t["model"] != aff["model"]:
                    continue
                n = next((c for c in cands if c.name == aff["node"]), None)
                if n is not None and n.is_loaded(t["model"]):
                    req.reason = "affinity"
                    rank(cands, t["model"], req)
                    return i, t, ctx, n
    if role.get("latency_first"):
        local_cands_seen = False
        for i, t, ctx, need, cands in per_tier:
            if t.get("cloud"):
                # Stufe 5: eine Cloud-Stufe hat keine Ladezeit, ist aber nie "warm". Sie gilt als warm, solange keine
                # lokale Stufe VOR ihr Kandidaten hat - so gewinnt ein kaltes lokales Modell weiter vor der Cloud (Geld),
                # eine Cloud-Stufe an der Spitze (Rollenwahl oder routing.execution: cloud) aber vor einem warmen lokalen.
                if cands and not local_cands_seen:
                    if req is not None:
                        req.reason = "cloud"
                    return i, t, ctx, rank(cands, t["model"], req)[0]
                continue
            if cands:
                local_cands_seen = True
            warm = [n for n in cands if n.is_loaded(t["model"])]
            if warm:
                if req is not None:
                    req.reason = "warm-first"
                return i, t, ctx, rank(warm, t["model"], req)[0]
    for i, t, ctx, need, cands in per_tier:
        if cands:
            if req is not None:
                req.reason = "rank"
            return i, t, ctx, rank(cands, t["model"], req)[0]
    return None


def wakeable_for(tiers):
    for t in tiers:
        for n in state.NODES.values():
            if n.state == "offline" and n.wol and n.mac and t["model"] in n.last_known_models:
                if time.time() - n.last_wake >= state.CFG.wol_cooldown_s:
                    return n
    return None


def any_online_node_with(model, prefer_loaded=True):
    ns = [n for n in state.NODES.values() if n.state != "offline" and model in n.models]
    if not ns:
        return None
    return rank(ns, model)[0]


def role_possible_when_free(role):
    """Koennte irgendein online-Knoten die Rolle bedienen, wenn er nicht belegt waere? (Modell vorhanden und
    Tier passt ins gesamte VRAM abzueglich der free-Reserve.) Unterscheidet 'Spiel laeuft' von 'Rolle kaputt'."""
    for t in role["tiers"]:
        if t.get("cloud"):
            ct = cloud.target_for(t)
            if ct is not None and ct.spec.get("enabled", True) and ct.api_key:
                return True
            continue
        for n in state.NODES.values():
            if n.state == "offline" or t["model"] not in n.models:
                continue
            if not n.vram_total_gib:
                return True
            if state.CFG.need_gib(t["model"], t["num_ctx"], n) <= n.vram_total_gib - state.CFG.reserve.get("free", 1.0):
                return True
    return False
