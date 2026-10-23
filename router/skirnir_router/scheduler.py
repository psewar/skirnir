"""Selection: role -> tiers -> matching nodes -> rank."""

import time

from . import admission, cloud, perf
from . import request as request_mod
from . import state


def known_concrete(name):
    """Specific model name held by any node (current or last seen). Returns the name with tag."""
    for cand in (name, f"{name}:latest"):
        if any(cand in n.models or cand in n.last_known_models or n.standby_switch(cand) for n in state.NODES.values()):
            return cand
    return None


def resolve_tiers(name, body):
    if name.startswith("workload:"):   # Alias from spec (§1): workload:standard == standard:latest
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
    """Candidates for a tier. mutate=False is the pure view (HA image, /metrics): no breaker switches to half_open."""
    ctx = min(client_ctx, tier["num_ctx"]) if client_ctx else tier["num_ctx"]
    need = None
    out = []
    allows = (lambda n: n.breaker_allows(now)) if mutate else (lambda n: n.breaker_would_allow(now))
    if tier.get("cloud"):   # Stage 5: the provider is the only "node" of this tier (guard checks request.tier_blocked)
        t = cloud.target_for(tier)
        if t is not None and t.name not in exclude and allows(t):
            out.append(t)
        return ctx, 0.0, out
    for n in state.NODES.values():
        if n.name in exclude or n.state == "offline" or not n.offers(tier["model"]):
            continue
        if now < n.draining_until:   # Agent update running: node draining, no new requests
            continue
        if n.state == "busy" and not tier["busy_ok"]:
            continue
        if not allows(n):   # Stage 3: nodes with open breaker stay out (half_open: one probe)
            continue
        need = state.CFG.need_gib(tier["model"], ctx, n)
        if n.releases_server_for(tier["model"]):
            # 0.16.3: the release stops the standby with all its models, and the server sizes itself to the card (its
            # own reserve) as it did before the hold - no budget to check, only a game (busy, above) or memory pressure.
            # 2026-10-24: with a budget the catalog's planning figure refused the release (28.8 > 28.5 GiB).
            if not n.mem_pressure:
                out.append(n)
            continue
        if n.standby_switch(tier["model"]):
            # 0.15.0: reachable only by holding the OpenAI-compatible server for the standby Ollama - a cold candidate
            # whose budget is the card once the server has stopped (score: modes.score.switch penalty)
            if not n.mem_pressure and need <= n.standby_budget_gib():
                out.append(n)
            continue
        lc = n.loaded_context(tier["model"])
        if n.is_loaded(tier["model"]) and (lc is None or lc >= ctx):
            # Already loaded with sufficient context: needs no extra VRAM, so no budget check.
            # The Heartbeat during/shortly after a response shows little free VRAM (compute buffer, KV); with the
            # Budget check failed on 2026-09-10 for the warm primary node 0.2 s after its own response,
            # and the follow-up request ran on the fallback tier of the other node.
            out.append(n)
            continue
        if n.mem_pressure:   # Agent >= 0.17.0: RAM tight - do not load cold (the loaded one above keeps answering)
            continue
        if need > n.budget_gib(now, tier["model"]):
            continue
        out.append(n)
    return ctx, need, out


def score(node, model):
    """Stage 3: weighted score (modes.score). The defaults mirror the old order - warm (100) before few
    running requests (10 per request; full node additionally -50) before free VRAM (share x2) before weight (x1) -
    and add measured speed (EWMA tok/s / 100 x2), recent error rate (x20) and breaker probe (-5)."""
    w = state.CFG.score
    s = w["warm"] if node.is_loaded(model) else 0.0
    s -= w["inflight"] * node.inflight
    if admission.saturated(node, model):   # Node full or model at its max_parallel limit
        s -= w["saturated"]
    gg = state.CFG.gpu_guard   # GPU guard: node under high load/stage 2 yields to a second node that can carry the tier
    if gg["enabled"] and node.guard_policy and node.guard_state() in ("high_load", "throttled"):   # CloudTarget: guard_policy False
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
    if not node.is_cloud and node.standby_switch(model):   # 0.15.0: the server must be held first (1-3 min both ways)
        s -= w["switch"]
    return round(s, 3)


def rank(nodes, model, req=None):
    scored = sorted(((score(n, model), n) for n in nodes), key=lambda x: (-x[0], x[1].name))
    if req is not None:
        req.candidates = [{"node": n.name, "score": s, "warm": n.is_loaded(model), "inflight": n.inflight, "state": n.state}
                          for s, n in scored]
    return [n for s, n in scored]


def loading_for(tiers, now, exclude=()):
    """A node that could be a candidate for one of the tiers and is currently loading a model (unexpired claim from
    announce_load): reason to wait instead of 503. Returns (node, loading model) or None."""
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
    """Interactive requests (voice commands) do not wait behind a running request if another local tier can serve
    immediately. Warm-first otherwise sticks to the busy warm node: with max_parallel 1 (qwen3.8, since 0.6.5) a
    voice command waited up to 60 s behind a minute-long agent request, although a free tier was ready on a second node.
    Order: free warm tier (tier rank), then free cold tier. Never cloud tiers (cost, data class -
    handled by the normal path). None = no free tier: proceed normally, then wait at the warm node."""
    local = [pt for pt in per_tier if not pt[1].get("cloud")]
    busy = {n.name for _i, t, _c, _n, cands in local for n in cands if n.is_loaded(t["model"]) and admission.saturated(n, t["model"])}
    if not busy:
        return None   # nothing warm is busy: the normal path (warm first) decides as before
    # Other nodes first: a fallback model on the busy node shared the GPU with the running request (both
    # slower) and occupied VRAM that the warm model might need - only if it is the only one that can, it may
    for other_only in (True, False):
        for warm_only in (True, False):
            for i, t, ctx, _need, cands in local:
                free = [n for n in cands if not admission.saturated(n, t["model"]) and (n.is_loaded(t["model"]) or not warm_only)
                        and (n.name not in busy or not other_only)]
                if free:
                    req.reason = "fallback"
                    return i, t, ctx, rank(free, t["model"], req)[0]
    return None


def choose(role, tiers, client_ctx, now, exclude=(), req=None, mutate=True):
    """Returns (tier_index, tier, ctx, node) or None. `req` (request.Routing) filters tiers by requirements,
    prefers tiers with desired capabilities and keeps a session on its warm node."""
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
        # Stage 6: triggered canary tier gets traffic even cold - otherwise a candidate model next to a
        # warm primary model would never see a request. If no node can serve it, proceed normally.
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
                # Stage 5: a cloud tier has no load time but is never "warm". It counts as warm as long as no
                # local tier BEFORE it has candidates - so a cold local model still wins over cloud (cost),
                # a cloud tier at the top (role selection or routing.execution: cloud) but before a warm local one.
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
    ns = [n for n in state.NODES.values() if n.state != "offline" and n.offers(model)]
    if not ns:
        return None
    return rank(ns, model)[0]


def role_possible_when_free(role):
    """Could any online node serve the role if it were not busy? (Model present and
    tier fits into total VRAM minus free reserve.) Distinguishes 'game running' from 'role broken'."""
    for t in role["tiers"]:
        if t.get("cloud"):
            ct = cloud.target_for(t)
            if ct is not None and ct.spec.get("enabled", True) and ct.api_key:
                return True
            continue
        for n in state.NODES.values():
            if n.state == "offline" or not n.offers(t["model"]):
                continue
            if not n.vram_total_gib:
                return True
            if state.CFG.need_gib(t["model"], t["num_ctx"], n) <= n.vram_total_gib - state.CFG.reserve.get("free", 1.0):
                return True
    return False


# --- Why no node: base codes for the client ------------------------------------------------------------------
# The router knows why a request gets no node; the client should not guess from the error text.
# Trigger 2026-09-29: a game occupied 5 GiB on gpu-desktop, qwen3.6@131k no longer fit the budget, the agent client got only
# "no node available" and reported "model provider failed". With code and Retry-After a client can wait and
# honestly state the cause. The codes are wire format (503 response, /v1/skirnir/availability).
GPU_BUSY = "gpu_busy"                 # Node busy (game) or foreign VRAM takes the space: temporary
NODE_OFFLINE = "node_offline"
NODE_DRAINING = "node_draining"       # Agent update running
NODE_FAILING = "node_failing"         # Breaker offen
VRAM_FULL = "vram_full"               # Space missing, without foreign VRAM being the cause (tail, reserve)
MODEL_TOO_LARGE = "model_too_large"   # does not fit on the empty card either: configuration error, waiting does not help
MODEL_MISSING = "model_missing"       # no node has the model
ATTEMPT_FAILED = "attempt_failed"     # Node was candidate, attempt failed (tried)
MEMORY_PRESSURE = "memory_pressure"   # Agent >= 0.17.0: RAM tight, router does not load cold there
NO_NODE = "no_node"

# When a client should retry (Retry-After). No entry: waiting does not help.
RETRY_AFTER_S = {GPU_BUSY: 30, NODE_DRAINING: 60, NODE_FAILING: 30, VRAM_FULL: 10, ATTEMPT_FAILED: 10, NODE_OFFLINE: 60,
                 MEMORY_PRESSURE: 60}


def _node_block(n, t, ctx, now):
    """Base code for why node `n` cannot serve tier `t` right now - or None (could serve)."""
    if n.state == "offline":
        return NODE_OFFLINE, {}
    if now < n.draining_until:
        return NODE_DRAINING, {}
    if n.state == "busy" and not t["busy_ok"]:
        info = {"busy_reason": n.busy_reason, "gpu_util": n.gpu_util, "foreign_vram_gib": round(n.foreign_vram_gib(), 2)}
        if n.busy_reason == "game" and n.game:
            info["game"] = n.game.get("name")
        return GPU_BUSY, info
    if not n.breaker_would_allow(now):
        return NODE_FAILING, {"breaker": n.breaker}
    lc = n.loaded_context(t["model"])
    if n.is_loaded(t["model"]) and (lc is None or lc >= ctx):
        return None, {}
    if n.mem_pressure:   # Like candidates_for: do not cold-load while main memory is tight
        return MEMORY_PRESSURE, {k: v for k, v in (n.memory or {}).items() if k in ("ram_available_gib", "commit_free_gib")}
    if n.releases_server_for(t["model"]):   # 0.16.3: like candidates_for - the release needs no budget
        return None, {}
    need = state.CFG.need_gib(t["model"], ctx, n)
    budget = n.standby_budget_gib() if n.standby_switch(t["model"]) else n.budget_gib(now, t["model"])
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
    """Per local tier x node with the model: why it is not serving. Pure view (no breaker changes)."""
    out = []
    for i, t in enumerate(tiers):
        if t.get("cloud"):
            continue
        ctx = min(client_ctx, t["num_ctx"]) if client_ctx else t["num_ctx"]
        for n in state.NODES.values():
            if not n.offers(t["model"]):
                continue
            code, info = _node_block(n, t, ctx, now)
            if code is None:
                if n.name not in exclude:
                    continue   # Could serve (e.g. became free): no blocker
                code = ATTEMPT_FAILED
            out.append(dict({"tier": i, "model": t["model"], "num_ctx": ctx, "node": n.name, "code": code}, **info))
    return out


def unavailable(tiers, client_ctx, now, exclude=()):
    """Overall verdict for a request without a node: {code, retry_after_s, blockers, detail}. gpu_busy takes precedence because it
    is the only one that resolves on its own and can be cleared by the human at the machine."""
    bl = blockers(tiers, client_ctx, now, exclude)
    codes = [b["code"] for b in bl]
    if not bl:
        code = MODEL_MISSING if not any(n.offers(t["model"]) for t in tiers for n in state.NODES.values()) else NO_NODE
    elif GPU_BUSY in codes:
        code = GPU_BUSY
    elif all(c == NODE_OFFLINE for c in codes):
        code = NODE_OFFLINE
    else:
        code = next(c for c in (ATTEMPT_FAILED, NODE_DRAINING, NODE_FAILING, MEMORY_PRESSURE, VRAM_FULL, MODEL_TOO_LARGE, NODE_OFFLINE)
                    if c in codes)
    return {"code": code, "retry_after_s": RETRY_AFTER_S.get(code), "blockers": bl, "detail": _detail(code, bl)}


def _detail(code, bl):
    """Short plain text for the code (English like all API texts); numbers are machine-readable in `blockers`."""
    b = next((x for x in bl if x["code"] == code), None)
    if code == GPU_BUSY and b is not None:
        if b.get("busy_reason") == "foreign_vram" and "need_gib" in b:
            return (f"GPU busy on {b['node']}: another program uses {b['foreign_vram_gib']:.1f} GiB, "
                    f"{b['model']} needs {b['need_gib']:.1f} GiB, {b['budget_gib']:.1f} GiB available")
        if b.get("busy_reason") == "game":
            return f"GPU busy on {b['node']}: a game is running" + (f" ({b['game']})" if b.get("game") else "")
        return f"GPU busy on {b['node']} ({b.get('busy_reason') or 'busy'}, util {b.get('gpu_util')} %)"
    if code == MEMORY_PRESSURE and b is not None:
        return (f"memory pressure on {b['node']}: {b.get('ram_available_gib')} GiB RAM and {b.get('commit_free_gib')} GiB commit "
                f"free, the router loads nothing there")
    if code == MODEL_MISSING:
        return "no node has this model"
    if b is not None:
        return f"{code.replace('_', ' ')} on {b['node']}"
    return code.replace("_", " ")
