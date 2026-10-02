"""Auswahl: Rolle -> Tiers -> passende Knoten -> Rang."""

import time

from . import admission, cloud, perf
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


def candidates_for(tier, client_ctx, now, exclude=(), mutate=True):
    """Kandidaten einer Stufe. mutate=False ist die reine Sicht (HA-Bild, /metrics): kein Breaker wechselt nach half_open."""
    ctx = min(client_ctx, tier["num_ctx"]) if client_ctx else tier["num_ctx"]
    need = None
    out = []
    allows = (lambda n: n.breaker_allows(now)) if mutate else (lambda n: n.breaker_would_allow(now))
    if tier.get("cloud"):   # Stufe 5: der Anbieter ist der einzige "Knoten" dieser Stufe (Schranken prueft request.tier_blocked)
        t = cloud.target_for(tier)
        if t is not None and t.name not in exclude and allows(t):
            out.append(t)
        return ctx, 0.0, out
    for n in state.NODES.values():
        if n.name in exclude or n.state == "offline" or tier["model"] not in n.models:
            continue
        if now < n.draining_until:   # Agent-Update laeuft: Knoten leert sich, keine neuen Anfragen
            continue
        if n.state == "busy" and not tier["busy_ok"]:
            continue
        if not allows(n):   # Stufe 3: Knoten mit offenem Breaker bleiben aussen vor (half_open: eine Probe)
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
    if admission.saturated(node, model):   # Knoten voll oder Modell an seiner max_parallel-Grenze
        s -= w["saturated"]
    gg = state.CFG.gpu_guard   # GPU-Schutz: Knoten in Hochlast/Stufe 2 weichen einem zweiten Knoten, der die Stufe tragen kann
    if gg["enabled"] and node.guard_policy and node.guard_state() in ("hochlast", "gedrosselt"):   # CloudTarget: guard_policy False
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


def loading_for(tiers, now, exclude=()):
    """Ein Knoten, der fuer eine der Stufen in Frage kaeme und gerade ein Modell laedt (unverfallener Anspruch aus
    announce_load): Grund zu warten statt 503. Liefert (Knoten, ladendes Modell) oder None."""
    for t in tiers:
        if t.get("cloud"):
            continue
        for n in state.NODES.values():
            if n.name in exclude or n.state == "offline" or t["model"] not in n.models:
                continue
            if n.state == "busy" and not t["busy_ok"]:
                continue
            for m, (_gib, deadline) in n.loading.items():
                if now < deadline and not n.is_loaded(m):
                    return n, m
    return None


def _interactive_pick(per_tier, req):
    """Interaktive Anfragen (Sprachbefehle) warten nicht hinter einer laufenden Anfrage, wenn eine andere lokale Stufe sofort
    bedienen kann. Warm zuerst haelt sonst am belegten warmen Knoten fest: mit max_parallel 1 (qwen3.8, seit 0.6.5) wartete ein
    Sprachbefehl bis zu 60 s hinter einer minutenlangen Agenten-Anfrage, obwohl auf einem zweiten Knoten eine freie Stufe bereitstand.
    Reihenfolge: freie warme Stufe (Rangfolge der Stufen), dann freie kalte Stufe. Cloud-Stufen nie (Kosten, Datenklasse -
    die regelt der normale Weg). None = keine freie Stufe: normal weiter, dann wird am warmen Knoten gewartet."""
    local = [pt for pt in per_tier if not pt[1].get("cloud")]
    busy = {n.name for _i, t, _c, _n, cands in local for n in cands if n.is_loaded(t["model"]) and admission.saturated(n, t["model"])}
    if not busy:
        return None   # nichts Warmes belegt: der normale Weg (warm zuerst) entscheidet wie bisher
    # Andere Knoten zuerst: ein Ausweichmodell auf dem belegten Knoten teilte sich die GPU mit der laufenden Anfrage (beide
    # langsamer) und belegte VRAM, das dem warmen Modell fehlen kann - erst wenn nur er kann, darf er
    for other_only in (True, False):
        for warm_only in (True, False):
            for i, t, ctx, _need, cands in local:
                free = [n for n in cands if not admission.saturated(n, t["model"]) and (n.is_loaded(t["model"]) or not warm_only)
                        and (n.name not in busy or not other_only)]
                if free:
                    req.reason = "ausweichen"
                    return i, t, ctx, rank(free, t["model"], req)[0]
    return None


def choose(role, tiers, client_ctx, now, exclude=(), req=None, mutate=True):
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
        ctx, need, cands = candidates_for(t, client_ctx, now, exclude, mutate)
        per_tier.append((i, t, ctx, need, cands))
    if req is not None and req.canary:
        # Stufe 6: ausgeloste Canary-Stufe bekommt den Verkehr auch kalt - sonst saehe ein Kandidatenmodell neben einem
        # warmen Hauptmodell nie eine Anfrage. Kann kein Knoten sie bedienen, geht es normal weiter.
        for i, t, ctx, _need, cands in per_tier:
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
            for i, t, ctx, _need, cands in per_tier:
                if t["model"] != aff["model"]:
                    continue
                n = next((c for c in cands if c.name == aff["node"]), None)
                if n is not None and n.is_loaded(t["model"]):
                    req.reason = "affinity"
                    rank(cands, t["model"], req)
                    return i, t, ctx, n
    if role.get("latency_first") and req is not None and req.priority == "interactive":
        pick = _interactive_pick(per_tier, req)
        if pick is not None:
            return pick
    if role.get("latency_first"):
        local_cands_seen = False
        for i, t, ctx, _need, cands in per_tier:
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
    for i, t, ctx, _need, cands in per_tier:
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


def any_online_node_with(model):
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


# --- Warum kein Knoten: Grund-Codes fuer den Client ------------------------------------------------------------------
# Der Router weiss, warum eine Anfrage keinen Knoten bekommt; der Client soll es nicht aus dem Fehlertext erraten.
# Anlass 2026-09-29: ein Spiel belegte auf gpu-desktop 5 GiB, qwen3.6@131k passte nicht mehr ins Budget, der Agent-Client bekam nur
# "no node available" und meldete "model provider failed". Mit Code und Retry-After kann ein Client warten und
# ehrlich sagen, woran es liegt. Die Codes sind Drahtformat (503-Antwort, /v1/skirnir/availability).
GPU_BUSY = "gpu_busy"                 # Knoten belegt (Spiel) oder fremdes VRAM nimmt den Platz: voruebergehend
NODE_OFFLINE = "node_offline"
NODE_DRAINING = "node_draining"       # Agent-Update laeuft
NODE_FAILING = "node_failing"         # Breaker offen
VRAM_FULL = "vram_full"               # Platz fehlt, ohne dass fremdes VRAM schuld ist (Nachlauf, Reserve)
MODEL_TOO_LARGE = "model_too_large"   # passt auch auf die leere Karte nicht: Konfigurationsfehler, Warten hilft nicht
MODEL_MISSING = "model_missing"       # kein Knoten hat das Modell
ATTEMPT_FAILED = "attempt_failed"     # Knoten war Kandidat, der Versuch scheiterte (tried)
NO_NODE = "no_node"

# Wann ein Client es wieder versuchen soll (Retry-After). Ohne Eintrag: Warten hilft nicht.
RETRY_AFTER_S = {GPU_BUSY: 30, NODE_DRAINING: 60, NODE_FAILING: 30, VRAM_FULL: 10, ATTEMPT_FAILED: 10, NODE_OFFLINE: 60}


def _node_block(n, t, ctx, now):
    """Grund-Code, warum Knoten `n` die Stufe `t` gerade nicht bedient - oder None (koennte bedienen)."""
    if n.state == "offline":
        return NODE_OFFLINE, {}
    if now < n.draining_until:
        return NODE_DRAINING, {}
    if n.state == "busy" and not t["busy_ok"]:
        return GPU_BUSY, {"busy_reason": n.busy_reason, "gpu_util": n.gpu_util,
                          "foreign_vram_gib": round(n.foreign_vram_gib(), 2)}
    if not n.breaker_would_allow(now):
        return NODE_FAILING, {"breaker": n.breaker}
    lc = n.loaded_context(t["model"])
    if n.is_loaded(t["model"]) and (lc is None or lc >= ctx):
        return None, {}
    need, budget = state.CFG.need_gib(t["model"], ctx, n), n.budget_gib(now, t["model"])
    if need <= budget:
        return None, {}
    foreign = n.foreign_vram_gib()
    info = {"need_gib": round(need, 2), "budget_gib": round(budget, 2), "foreign_vram_gib": round(foreign, 2)}
    if n.vram_total_gib and need > n.vram_total_gib - state.CFG.reserve.get("free", 1.0):
        return MODEL_TOO_LARGE, info
    if foreign > 0 and need <= budget + foreign:
        return GPU_BUSY, dict(info, busy_reason="foreign_vram", gpu_util=n.gpu_util)
    return VRAM_FULL, info


def blockers(tiers, client_ctx, now, exclude=()):
    """Je lokale Stufe x Knoten mit dem Modell: warum er nicht bedient. Reine Sicht (kein Breaker-Wechsel)."""
    out = []
    for i, t in enumerate(tiers):
        if t.get("cloud"):
            continue
        ctx = min(client_ctx, t["num_ctx"]) if client_ctx else t["num_ctx"]
        for n in state.NODES.values():
            if t["model"] not in n.models:
                continue
            code, info = _node_block(n, t, ctx, now)
            if code is None:
                if n.name not in exclude:
                    continue   # koennte bedienen (z. B. frei geworden): kein Hindernis
                code = ATTEMPT_FAILED
            out.append(dict({"tier": i, "model": t["model"], "num_ctx": ctx, "node": n.name, "code": code}, **info))
    return out


def unavailable(tiers, client_ctx, now, exclude=()):
    """Gesamturteil fuer eine Anfrage ohne Knoten: {code, retry_after_s, blockers, detail}. gpu_busy geht vor, weil es
    das einzige ist, das von selbst vergeht und das der Mensch am Rechner selbst aufheben kann."""
    bl = blockers(tiers, client_ctx, now, exclude)
    codes = [b["code"] for b in bl]
    if not bl:
        code = MODEL_MISSING if not any(t["model"] in n.models for t in tiers for n in state.NODES.values()) else NO_NODE
    elif GPU_BUSY in codes:
        code = GPU_BUSY
    elif all(c == NODE_OFFLINE for c in codes):
        code = NODE_OFFLINE
    else:
        code = next(c for c in (ATTEMPT_FAILED, NODE_DRAINING, NODE_FAILING, VRAM_FULL, MODEL_TOO_LARGE, NODE_OFFLINE)
                    if c in codes)
    return {"code": code, "retry_after_s": RETRY_AFTER_S.get(code), "blockers": bl, "detail": _detail(code, bl)}


def _detail(code, bl):
    """Kurzer Klartext zum Code (englisch wie alle API-Texte); die Zahlen stehen maschinenlesbar in `blockers`."""
    b = next((x for x in bl if x["code"] == code), None)
    if code == GPU_BUSY and b is not None:
        if b.get("busy_reason") == "foreign_vram" and "need_gib" in b:
            return (f"GPU busy on {b['node']}: another program uses {b['foreign_vram_gib']:.1f} GiB, "
                    f"{b['model']} needs {b['need_gib']:.1f} GiB, {b['budget_gib']:.1f} GiB available")
        return f"GPU busy on {b['node']} ({b.get('busy_reason') or 'busy'}, util {b.get('gpu_util')} %)"
    if code == MODEL_MISSING:
        return "no node has this model"
    if b is not None:
        return f"{code.replace('_', ' ')} on {b['node']}"
    return code.replace("_", " ")
