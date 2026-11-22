#!/usr/bin/env python3
"""Self-test (router 0.37.1, ADR-0108): one node assessment for selection and explanation.

Until router 0.37.1 the scheduler judged a node twice: candidates_for decided whether it may serve a tier, and _node_block
("Like candidates_for") explained to the client why not. The two mirrored each other by hand and differed in one corner
(the held server's model loaded while the standby serves: the selection refused under memory pressure, the explanation
said the node could serve). Both now ask scheduler.assess. This test walks a matrix of node states (offline, free, busy
with a game or GPU load, draining, breaker closed, open, due, half-open, VRAM fit and no fit, foreign VRAM, model too large,
warm, standby switch, release of the held server with and without the agent's RAM block, memory pressure) and asserts that
the selection, the explanation and the blockers agree, that the selection changes no node, and that probe_breakers moves
exactly the breakers the selection reaches. Runs without router process, network or agent."""
import itertools
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import cloud, config, nodes, request, scheduler, state   # noqa: E402

FAILS = []
PASSES = [0]


def check(name, cond, info="", quiet=False):
    if cond and quiet:
        PASSES[0] += 1
        return
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
NEED = {"small:8b": 6.0, "mid:27b": 20.0, "big:35b": 26.0, "giant:1t": 60.0, "server-moe": 28.8}
state.CFG.need_gib = lambda model, ctx, node=None: NEED[model]
JOURNAL = []
state.remember = JOURNAL.append
CARD = 31.8
CTX = 32768


class FakeTunnel:
    pass


def node(way, st, busy_reason, draining, breaker, mem_pressure, now):
    """A synthetic node reaching the tier's model in the given way. Baseline 1 GiB unless stated."""
    openai = way.startswith(("standby", "release"))
    spec = {"vram_total_gib": CARD}
    if openai:
        spec.update(upstream_api="openai", upstream_releasable=True, upstream_fallback=True)
    n = nodes.Node("gpu-test", spec)
    n.hb_ts, n.foreign_baseline_gib, n.children_vram_gib = now, 1.0, 0.0
    ollama, foreign = 0.0, 0.0

    def install(*models, openai_models=()):
        n.models = set(models) | set(openai_models)
        n.digest_of = {m: f"sha256:{m}" for m in models}
        n.digest_of.update({m: f"openai:{m}" for m in openai_models})

    def load(m, gib, ctx):
        n.loaded[m] = gib
        n.loaded_digest[n.digest_of[m]] = gib
        n.loaded_ctx[n.digest_of[m]] = ctx

    if way == "cold_fit":
        install("mid:27b")
    elif way == "cold_full":            # a large desktop baseline leaves too little, nothing foreign
        install("big:35b")
        n.foreign_baseline_gib = 8.0
    elif way == "cold_foreign":         # another program takes the space
        install("big:35b")
        foreign = 6.0
    elif way == "too_large":
        install("giant:1t")
    elif way in ("warm", "warm_small_ctx"):
        install("mid:27b", "big:35b")
        load("mid:27b", 20.0, CTX if way == "warm" else 4096)
        ollama, foreign = 20.0, 9.0      # little free VRAM: the warm model needs none
    elif way in ("standby_fit", "standby_no_fit"):   # server answering, the standby has the model in its store
        install(openai_models=("server-moe",))
        load("server-moe", 0.0, CTX)
        n.children_vram_gib = 28.0       # the server's memory counts as the agent's child
        n.standby_models = {"mid:27b", "big:35b"}
        n.tunnel = FakeTunnel()
        foreign = 0.0 if way == "standby_fit" else 6.0
    elif way in ("release", "release_blocked", "release_and_loaded"):   # standby serves, server held
        install("mid:27b", openai_models=("server-moe",))
        load("mid:27b", 20.0, CTX)
        if way == "release_and_loaded":   # the corner where the two functions used to differ
            load("server-moe", 0.0, CTX)
        ollama = 20.0
        n.upstream_serving, n.upstream_state = "ollama", "held"
        n.tunnel = FakeTunnel()
        if way == "release_blocked":
            n.upstream_blocked = {"reason": "ram", "ram_available_gib": 3.0, "ram_total_gib": 64.0, "need_gib": 30.0,
                                  "limit_percent": 90}
    n.vram_used_gib = ollama + n.children_vram_gib + n.foreign_baseline_gib + foreign
    n.vram_free_gib = CARD - n.vram_used_gib
    n.state, n.busy_reason = st, busy_reason
    n.game = {"running": True, "name": "synthetic-game.exe"} if busy_reason == "game" else None
    n.draining_until = now + 60 if draining else 0.0
    kind, n.inflight = breaker
    n.breaker = "open" if kind.startswith("open") else kind
    n.breaker_until = now + 100 if kind == "open" else (now - 1 if kind == "open_due" else 0.0)
    n.mem_pressure = mem_pressure
    n.memory = {"ram_available_gib": 2.5, "commit_free_gib": 1.8, "ram_total_gib": 64.0} if mem_pressure else None
    return n


TIER_MODEL = {"cold_fit": "mid:27b", "cold_full": "big:35b", "cold_foreign": "big:35b", "too_large": "giant:1t",
              "warm": "mid:27b", "warm_small_ctx": "mid:27b", "standby_fit": "mid:27b", "standby_no_fit": "big:35b",
              "release": "server-moe", "release_blocked": "server-moe", "release_and_loaded": "server-moe"}
STATES = [("offline", ""), ("free", ""), ("busy", "game"), ("busy", "gpu_util")]
BREAKERS = [("closed", 0), ("open", 0), ("open_due", 0), ("open_due", 1), ("half_open", 0), ("half_open", 1)]


def breaker_view(n):
    return n.breaker, n.breaker_until


# --- The instrument first: each way reaches the model the way its name says ---
now = time.time()
for way, model in TIER_MODEL.items():
    n = node(way, "free", "", False, ("closed", 0), False, now)
    check(f"setup {way}: node offers the tier's model", n.offers(model))
check("setup: standby ways are standby switches", all(node(w, "free", "", False, ("closed", 0), False, now).standby_switch(TIER_MODEL[w])
                                                      for w in ("standby_fit", "standby_no_fit")))
check("setup: release ways release the held server", all(node(w, "free", "", False, ("closed", 0), False, now).releases_server_for("server-moe")
                                                         for w in ("release", "release_blocked", "release_and_loaded")))
check("setup: the corner way has the server's model loaded while the standby serves",
      node("release_and_loaded", "free", "", False, ("closed", 0), False, now).is_loaded("server-moe"))

# --- Expected codes at representative points (free node, closed breaker, no memory pressure, tier not busy_ok) ---
EXPECT = {"cold_fit": None, "cold_full": scheduler.VRAM_FULL, "cold_foreign": scheduler.GPU_BUSY,
          "too_large": scheduler.MODEL_TOO_LARGE, "warm": None, "standby_fit": None, "standby_no_fit": scheduler.GPU_BUSY,
          "release": None, "release_blocked": scheduler.MEMORY_PRESSURE, "release_and_loaded": None}
for way, want in EXPECT.items():
    n = node(way, "free", "", False, ("closed", 0), False, now)
    code, info = scheduler.assess(n, {"model": TIER_MODEL[way], "num_ctx": CTX, "busy_ok": False}, CTX, now)
    check(f"{way}: code {want}", code == want, f"{code} {info}")
tier = {"model": "big:35b", "num_ctx": CTX, "busy_ok": False}
code, info = scheduler.assess(node("cold_foreign", "free", "", False, ("closed", 0), False, now), tier, CTX, now)
check("foreign VRAM: busy_reason foreign_vram with the numbers", info.get("busy_reason") == "foreign_vram"
      and info.get("need_gib") == 26.0 and info.get("foreign_vram_gib") == 6.0, str(info))
code, info = scheduler.assess(node("release_blocked", "free", "", False, ("closed", 0), False, now),
                              {"model": "server-moe", "num_ctx": CTX, "busy_ok": False}, CTX, now)
check("release blocked by the agent: its RAM figures, marked agent", info.get("agent") is True and info.get("need_gib") == 30.0, str(info))
code, info = scheduler.assess(node("cold_fit", "busy", "game", False, ("closed", 0), False, now),
                              {"model": "mid:27b", "num_ctx": CTX, "busy_ok": False}, CTX, now)
check("game: GPU_BUSY names the game", code == scheduler.GPU_BUSY and info.get("game") == "synthetic-game.exe", f"{code} {info}")
code, _ = scheduler.assess(node("cold_fit", "busy", "game", False, ("closed", 0), False, now),
                           {"model": "mid:27b", "num_ctx": CTX, "busy_ok": True}, CTX, now)
check("game, tier busy_ok: no code", code is None, str(code))
for kind, want in ((("open", 0), scheduler.NODE_FAILING), (("open_due", 0), None), (("half_open", 0), None),
                   (("half_open", 1), scheduler.NODE_FAILING)):
    code, _ = scheduler.assess(node("cold_fit", "free", "", False, kind, False, now), {"model": "mid:27b", "num_ctx": CTX, "busy_ok": False},
                               CTX, now)
    check(f"breaker {kind}: code {want}", code == want, str(code))
for way, want in (("cold_fit", scheduler.MEMORY_PRESSURE), ("warm", None), ("standby_fit", scheduler.MEMORY_PRESSURE),
                  ("release", scheduler.MEMORY_PRESSURE), ("release_and_loaded", scheduler.MEMORY_PRESSURE)):
    code, _ = scheduler.assess(node(way, "free", "", False, ("closed", 0), True, now),
                               {"model": TIER_MODEL[way], "num_ctx": CTX, "busy_ok": False}, CTX, now)
    check(f"memory pressure, {way}: code {want}", code == want, str(code))

# --- The matrix: selection, explanation and blockers agree; the selection is pure; the probe moves what it reaches ---
combos = 0
for way, (st, reason), busy_ok, draining, breaker, mem_pressure in itertools.product(
        TIER_MODEL, STATES, (False, True), (False, True), BREAKERS, (False, True)):
    combos += 1
    now = time.time()
    n = node(way, st, reason, draining, breaker, mem_pressure, now)
    state.NODES = {n.name: n}
    t = {"model": TIER_MODEL[way], "num_ctx": CTX, "busy_ok": busy_ok}
    label = f"{way}/{st}:{reason}/busy_ok={busy_ok}/draining={draining}/breaker={breaker}/mem={mem_pressure}"
    before, JOURNAL[:] = breaker_view(n), []
    code, info = scheduler.assess(n, t, CTX, now)
    _ctx, cands = scheduler.candidates_for(t, None, now)
    check(f"agree {label}", (n in cands) == (code is None), f"candidate={n in cands} code={code} {info}", quiet=True)
    bl = scheduler.blockers([t], None, now)
    check(f"blockers agree {label}", [b["code"] for b in bl] == ([] if code is None else [code]), str(bl), quiet=True)
    if code is not None:
        check(f"blocker carries the facts {label}", all(bl[0].get(k) == v for k, v in info.items()), str(bl), quiet=True)
    excluded = scheduler.blockers([t], None, now, exclude={n.name})
    check(f"excluded node {label}", [b["code"] for b in excluded] == [code or scheduler.ATTEMPT_FAILED], str(excluded), quiet=True)
    role = {"name": "r", "tiers": [t], "latency_first": True}
    pick = scheduler.choose(role, [t], None, now)
    check(f"choose agrees {label}", (pick is not None) == (code is None), str(pick), quiet=True)
    check(f"selection is pure {label}", breaker_view(n) == before and JOURNAL == [], f"{breaker_view(n)} {JOURNAL}", quiet=True)
    scheduler.probe_breakers([t], now, exclude={n.name})
    check(f"probe skips an excluded node {label}", breaker_view(n) == before and JOURNAL == [], str(JOURNAL), quiet=True)
    reaches = scheduler._state_gate(n, t, now)[0] is None
    scheduler.probe_breakers([t], now)
    due = breaker[0] == "open_due"
    if due and reaches:
        check(f"probe moves the due breaker {label}", n.breaker == "half_open"
              and [e.get("state") for e in JOURNAL if e.get("event") == "breaker"] == ["half_open"], str(JOURNAL), quiet=True)
        code2, _ = scheduler.assess(n, t, CTX, now)
        check(f"probe does not change the verdict {label}", code2 == code, f"{code} -> {code2}", quiet=True)
    else:
        check(f"probe leaves the breaker {label}", breaker_view(n) == before and JOURNAL == [], f"{breaker_view(n)} {JOURNAL}", quiet=True)
check(f"matrix: {combos} node states, selection, explanation and probe agree", not FAILS, f"{PASSES[0]} checks")

# --- The corner that used to disagree: now refused by both, with the reason ---
now = time.time()
n = node("release_and_loaded", "free", "", False, ("closed", 0), True, now)
state.NODES = {n.name: n}
t = {"model": "server-moe", "num_ctx": CTX, "busy_ok": False}
check("corner: no candidate under memory pressure (unchanged)", scheduler.candidates_for(t, None, now)[1] == [])
check("corner: explanation memory_pressure (before 0.37.1: 'could serve')",
      [b["code"] for b in scheduler.blockers([t], None, now)] == [scheduler.MEMORY_PRESSURE])

# --- Cloud tier: the provider's breaker, the request's block decides whether the probe reaches it ---
now = time.time()
target = cloud.CloudTarget("synthetic", {"enabled": True})
target.api_key = "test-only"
target.breaker, target.breaker_until = "open", now - 1
state.CLOUD, state.NODES = {"synthetic": target}, {}
ct = {"model": "cloud-model", "num_ctx": CTX, "busy_ok": True, "cloud": "synthetic"}
JOURNAL[:] = []
check("cloud: candidates_for lets the due provider through without moving it",
      scheduler.candidates_for(ct, None, now)[1] == [target] and target.breaker == "open" and JOURNAL == [])
req = request.Routing()
req.cloud_block = "cloud not allowed for this request"
scheduler.probe_breakers([ct], now, req=req)
check("cloud: a tier the request blocks is not probed", target.breaker == "open" and JOURNAL == [])
scheduler.probe_breakers([ct], now, exclude={target.name})
check("cloud: an excluded provider is not probed", target.breaker == "open" and JOURNAL == [])
scheduler.probe_breakers([ct], now)
check("cloud: otherwise the due provider moves to half_open", target.breaker == "half_open" and len(JOURNAL) == 1)

# --- Budgets: one formula for prewarm and standby, the empty card for residency and the too-large test ---
now = time.time()
n = node("cold_foreign", "free", "", False, ("closed", 0), False, now)
check("card_gib: card minus the reserve of the given state", nodes.card_gib(n, "free") == CARD - state.CFG.reserve["free"]
      and nodes.card_gib(n, "busy") == CARD - state.CFG.reserve["busy"])
check("card_budget_gib: card minus reserve, baseline and foreign VRAM",
      abs(n.card_budget_gib("free") - (CARD - state.CFG.reserve["free"] - 1.0 - 6.0)) < 1e-9, str(n.card_budget_gib("free")))
check("standby_budget_gib is card_budget_gib with the node's own reserve",
      n.standby_budget_gib() == n.card_budget_gib("free") and (setattr(n, "state", "busy") or n.standby_budget_gib() == n.card_budget_gib("busy")))
n = node("warm", "free", "", False, ("closed", 0), False, now)
n.inflight_models["mid:27b"] = 1
check("answering_vram_gib and ollama_vram_gib share the measured-occupancy sum", n.answering_vram_gib() == n.ollama_vram_gib() == 20.0,
      f"{n.answering_vram_gib()} {n.ollama_vram_gib()}")
n.inflight_models["mid:27b"] = 0
check("answering_vram_gib: nothing answering, nothing counted", n.answering_vram_gib() == 0.0)

print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
