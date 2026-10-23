#!/usr/bin/env python3
"""Self-test (Router 0.15.0): switching an OpenAI-compatible node to its standby Ollama for a request.

Trigger 2026-10-23: while Strata held the card, the node listed only Strata's model; the UI offered other models from the
catalog, but no request for them found a node. Now the agent reports the standby Ollama's models from its model store
(heartbeat standby_models), the scheduler offers such a node as a cold candidate with a switch penalty, and the request
holds the server and waits until the standby serves. Runs without router process, network or agent."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, openai_node, poll, scheduler, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.mqtt = None
state.spawn = lambda coro: coro.close()
STRATA, OLLAMA_MODEL = "synthetic-moe-flash", "example-dense:27b"
state.CFG.models[OLLAMA_MODEL] = {"weights_gib": 17.0, "kv_gib_per_1k": 0.05, "real": True}
state.CFG.models[STRATA] = {"weights_gib": 0.0}
TIER = {"model": OLLAMA_MODEL, "num_ctx": 8192, "busy_ok": False}


class FakeTunnel:
    def __init__(self):
        self.sent = []


def openai_node_with_standby():
    now = time.time()
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "upstream_api": "openai", "upstream_releasable": True,
                                    "upstream_fallback": True})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 2, 31.8, 26.0, 5.8
    n.tunnel = FakeTunnel()
    n.children_vram_gib = 24.0   # the server's memory is the agent's own child (agent fact children_vram_mib)
    n.models, n.loaded, n.digest_of = {STRATA}, {STRATA: 0.0}, {STRATA: "openai:" + STRATA}
    return n


def ollama_node(model):
    now = time.time()
    n = nodes.Node("gpu-second", {"vram_total_gib": 32.0})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 1, 32.0, 1.0, 31.0
    n.models = {model}
    return n


# 1) The agent's fact
n = openai_node_with_standby()
state.NODES = {n.name: n}
check("without the fact no switch is offered", not n.standby_switch(OLLAMA_MODEL) and not n.offers(OLLAMA_MODEL))
poll.apply_heartbeat(n, {"gpu_util_pct": 2, "vram_total_mib": 32560, "vram_used_mib": 26600, "vram_free_mib": 5960,
                         "children_vram_mib": {"strata": 24576}, "standby_models": [OLLAMA_MODEL, "", 7, "other:8b"]}, time.time())
check("heartbeat standby_models -> set of names (junk dropped)", n.standby_models == {OLLAMA_MODEL, "other:8b"}, str(n.standby_models))
check("snapshot lists the switchable models", n.snapshot(time.time())["standby_models"] == ["example-dense:27b", "other:8b"])

# 2) Scheduler: a cold candidate, budget = card once the server stopped, penalty against a node that has the model
check("node offers the standby model while the server answers", n.standby_switch(OLLAMA_MODEL) and n.offers(OLLAMA_MODEL))
_, _, cands = scheduler.candidates_for(TIER, None, time.time())
check("candidates_for: the switchable node is a candidate although only 5.8 GiB are free now", n in cands, str([c.name for c in cands]))
laptop = ollama_node(OLLAMA_MODEL)
state.NODES = {n.name: n, laptop.name: laptop}
_, _, cands = scheduler.candidates_for(TIER, None, time.time())
best = scheduler.rank(cands, OLLAMA_MODEL)[0]
check("a node that has the model ranks before one that would have to switch", best is laptop,
      f"{[(c.name, scheduler.score(c, OLLAMA_MODEL)) for c in cands]}")
state.NODES = {n.name: n}
check("no 'model_missing' while a switch can serve it", scheduler.unavailable([TIER], None, time.time(), exclude=(n.name,))["code"]
      != scheduler.MODEL_MISSING)

# 3) When no switch applies
n.state = "busy"
_, _, cands = scheduler.candidates_for(TIER, None, time.time())
check("busy node (game) is no candidate for a non-busy_ok tier", n not in cands)
n.state = "free"
n.upstream_serving = openai_node.SERVING_OLLAMA
check("standby already serving: no switch (its models are in `models` then)", not n.standby_switch(OLLAMA_MODEL))
n.upstream_serving = ""
n.upstream_fallback = False
check("agent without upstream_fallback: no switch", not n.standby_switch(OLLAMA_MODEL))
n.upstream_fallback = True
state.CFG.models[OLLAMA_MODEL]["weights_gib"] = 40.0
_, _, cands = scheduler.candidates_for(TIER, None, time.time())
check("model larger than the card: no candidate", n not in cands)
state.CFG.models[OLLAMA_MODEL]["weights_gib"] = 17.0


# 4) ensure_standby: hold, wait for the standby, give up in time
async def scenario(agent_answers):
    sent, tasks = [], []

    async def fake_order(node, what, reason):
        sent.append((what, reason))
        node.upstream_order = what
        if agent_answers:   # the agent stops the server and starts the standby, which lists its models
            async def later():
                await asyncio.sleep(0.3)
                node.upstream_serving = openai_node.SERVING_OLLAMA
                node.models = {STRATA, OLLAMA_MODEL}
            tasks.append(asyncio.ensure_future(later()))
        return True
    real = openai_node.order
    openai_node.order = fake_order
    try:
        ok = await openai_node.ensure_standby(n, OLLAMA_MODEL, timeout_s=3)
    finally:
        openai_node.order = real
    return ok, sent


n.models, n.upstream_serving = {STRATA}, ""
ok, sent = asyncio.run(scenario(True))
check("ensure_standby: hold sent with the request as reason, True once the standby serves the model",
      ok and sent == [("hold", f"request {OLLAMA_MODEL}")], f"{ok} {sent}")
check("... then the request goes the Ollama way (serves_openai False for the standby model)", not n.serves_openai(OLLAMA_MODEL))
check("... and the server's model would release it again (serves_openai True)", n.serves_openai(STRATA))
n.models, n.upstream_serving = {STRATA}, ""
t0 = time.time()
ok, _ = asyncio.run(scenario(False))
check("ensure_standby: no standby in time -> False (next candidate), not a hang", ok is False and time.time() - t0 < 5)

print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
