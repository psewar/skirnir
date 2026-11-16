#!/usr/bin/env python3
"""Self-test (Router 0.32.1, ADR-0097): the interactive fallback does not evict the model that is answering.

Trigger 2026-11-16 13:36: the role's first model had just been loaded and answered one request (max_parallel 1). A voice
command took the interactive fallback, which loaded a 20.7 GiB lower tier cold on the same node: the budget counted the
answering model's memory as evictable, so Ollama evicted the first model as soon as its request ended. The requests
waiting for it then waited for the lower tier, reached their deadline and went to the cloud. Now a cold fallback on the
busy node must fit beside the answering model; otherwise the request waits for the warm model as before. Runs without
router process, network or agent."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, request, scheduler, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.mqtt = None
state.spawn = lambda coro: coro.close()
FIRST, MID, SMALL = "example-dense:27b", "example-moe:35b", "example-small:8b"
state.CFG.models[FIRST] = {"weights_gib": 17.0, "kv_gib_per_1k": 0.05, "real": True, "max_parallel": 1}
state.CFG.models[MID] = {"weights_gib": 20.0, "kv_gib_per_1k": 0.02, "real": True}
state.CFG.models[SMALL] = {"weights_gib": 5.0, "kv_gib_per_1k": 0.01, "real": True}
TIERS = [{"model": FIRST, "num_ctx": 8192, "busy_ok": False},
         {"model": MID, "num_ctx": 8192, "busy_ok": False},
         {"model": SMALL, "num_ctx": 8192, "busy_ok": False}]
ROLE = {"name": "assist", "latency_first": True, "tiers": TIERS}


def node(name, answering=True, free_gib=12.0):
    """A 32 GiB node with FIRST loaded (17.4 GiB); `answering`: one request on it (FIRST is at max_parallel 1)."""
    now = time.time()
    n = nodes.Node(name, {"vram_total_gib": 32.0})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 40, 32.0, 32.0 - free_gib, free_gib
    n.models = {FIRST, MID, SMALL}
    n.loaded = {FIRST: 17.4}
    if answering:
        n.inflight, n.inflight_models[FIRST] = 1, 1
    return n


def choose(tiers=TIERS):
    req = request.Routing.fresh()
    req.priority = "interactive"
    return scheduler.choose(dict(ROLE, tiers=tiers), tiers, None, time.time(), (), req), req


# 0) The instrument: the busy node's budget still counts the answering model as evictable, so MID is a cold candidate
n = node("gpu-node")
state.NODES = {n.name: n}
now = time.time()
_, _, cands = scheduler.candidates_for(TIERS[1], None, now)
check("setup: the 20 GiB tier is a cold candidate on the busy node (budget counts the answering model)", cands == [n],
      f"budget {n.budget_gib(now, MID):.1f} need {state.CFG.need_gib(MID, 8192, n):.1f}")
check("setup: the first model is warm and at its max_parallel limit", n.is_loaded(FIRST) and scheduler.admission.saturated(n, FIRST))
check("answering_vram_gib counts the answering model only", abs(n.answering_vram_gib() - 17.4) < 0.01, str(n.answering_vram_gib()))

# 1) The fix: no cold fallback that needs the answering model's memory - the request stays on the warm first tier (waits)
pick, req = choose(TIERS[:2])
check("cold fallback that would evict the answering model is not taken; warm first tier instead",
      pick is not None and pick[0] == 0 and req.reason == "warm-first", f"{pick and pick[0]} {req.reason}")

# 2) A cold fallback that fits beside the answering model is still taken at once (the voice-command rule stands)
pick, req = choose()
check("cold fallback that fits beside the answering model: taken (reason fallback)",
      pick is not None and pick[0] == 2 and req.reason == "fallback", f"{pick and pick[0]} {req.reason}")

# 3) A warm fallback on the busy node is taken (nothing is loaded)
n = node("gpu-node")
n.loaded[MID] = 9.0
n.vram_free_gib = 3.0
state.NODES = {n.name: n}
pick, req = choose(TIERS[:2])
check("warm fallback on the busy node: taken", pick is not None and pick[0] == 1 and req.reason == "fallback",
      f"{pick and pick[0]} {req.reason}")

# 4) A cold fallback on ANOTHER node is unaffected (it evicts nothing that answers)
busy, other = node("gpu-node"), node("gpu-second", answering=False, free_gib=31.0)
other.loaded, other.vram_free_gib = {}, 31.0
state.NODES = {busy.name: busy, other.name: other}
pick, req = choose(TIERS[:2])
check("cold fallback on another node: taken there", pick is not None and pick[3] is other and req.reason == "fallback",
      f"{pick and (pick[0], pick[3].name)} {req.reason}")

# 5) Nothing answering: the normal path decides as before (no fallback)
n = node("gpu-node", answering=False)
state.NODES = {n.name: n}
pick, req = choose(TIERS[:2])
check("first model idle: warm first tier (no fallback)", pick is not None and pick[0] == 0 and req.reason == "warm-first",
      f"{pick and pick[0]} {req.reason}")

print(f"\n{len(FAILS)} FAIL" if FAILS else "\nall PASS")
sys.exit(1 if FAILS else 0)
