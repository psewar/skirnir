#!/usr/bin/env python3
"""Self-test (router 0.16.2/0.16.3): a request may release the held server while the standby Ollama serves.

Trigger 2026-10-24: outside the night window the standby served with a 27b model loaded (25.7 GiB), and a request for
the held server's model was refused, first with "vram full" (need compared with what was left next to the standby's
models), then, with the whole card as budget, with "GPU busy" (catalog planning figure 28.8 > 28.5 GiB). The release
stops the standby with all its models and the server sizes itself to the card as before the hold, so no budget is
checked (0.16.3), only a game or memory pressure. Also checks that a plain Ollama node keeps its old budget. Runs
without network, agent or server."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, poll, scheduler, state  # noqa: E402

FAILS = []
BIG = "big-moe"
DENSE = "dense:27b"
NEED = {BIG: 28.8, DENSE: 25.7}   # 28.8 = the router's planning figure for the server on 2026-10-24


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class FakeTunnel:
    def __init__(self):
        self.lock = asyncio.Lock()


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.prewarm_on_online = False
real_need = state.CFG.need_gib
state.CFG.need_gib = lambda model, ctx, node=None: NEED.get(model) or real_need(model, ctx, node)
poll.evaluate = lambda node, now: None
state.spawn = lambda coro: coro.close()   # capability fetches and similar background work are not part of this test
TIER = {"model": BIG, "num_ctx": 131072, "busy_ok": False}


def standby_node():
    """The night of 2026-10-24: 31.8 GiB card, standby serves with DENSE loaded, speech-to-text child 2.2 GiB, desktop
    baseline 2.0 GiB, another program 0.3 GiB."""
    n = nodes.Node("strata-box", {"vram_total_gib": 31.8, "upstream_api": "openai", "upstream_releasable": True,
                                  "upstream_fallback": True})
    n.tunnel = FakeTunnel()
    msg = {"up": True, "rev": 1, "serving": "ollama", "upstream_state": "held",
           "tags": [{"name": DENSE, "digest": "sha256:aa", "size": 17 << 30},
                    {"name": BIG, "digest": f"openai:{BIG}", "size": 0}],
           "ps": [{"name": DENSE, "digest": "sha256:aa", "size_vram": int(25.7 * (1 << 30))}]}
    poll.apply_ollama_push(n, msg, time.time())
    n.state, n.hb_ts = "free", time.time()
    n.children_vram_gib, n.foreign_baseline_gib = 2.2, 2.0
    n.vram_used_gib = 25.7 + 2.2 + 2.0 + 0.3
    n.vram_free_gib = 31.8 - n.vram_used_gib
    state.NODES = {n.name: n}
    return n


now = time.time()
n = standby_node()
# The instrument first: the setup must reproduce both refusals (need above the left-over budget and above the card)
check("setup: standby serves, DENSE loaded, BIG is the held server's model",
      n.upstream_serving == "ollama" and n.is_loaded(DENSE) and n.serves_openai(BIG) and not n.serves_openai(DENSE))
check("setup: the planning figure exceeds the left-over budget and the whole card (as on 2026-10-24)",
      n.budget_gib(now, BIG) < n.standby_budget_gib() < NEED[BIG] and n.foreign_vram_gib() > 0,
      f"left over {n.budget_gib(now, BIG):.2f}, card {n.standby_budget_gib():.2f}, need {NEED[BIG]}")

check("releases_server_for: the server's model while the standby serves", n.releases_server_for(BIG))
check("releases_server_for: not for the standby's own model", not n.releases_server_for(DENSE))
_, _, cands = scheduler.candidates_for(TIER, None, now)
check("the node is a candidate for the server's model (no vram_full, no gpu_busy)", cands == [n], str([c.name for c in cands]))
code, info = scheduler._node_block(n, TIER, 131072, now)
check("reason code: none (it could serve)", code is None, f"{code} {info}")

# Memory pressure still blocks the release (the server would fill RAM further)
n = standby_node()
n.mem_pressure = True
_, _, cands = scheduler.candidates_for(TIER, None, time.time())
check("memory pressure: no candidate", cands == [])
code, _ = scheduler._node_block(n, TIER, 131072, time.time())
check("memory pressure: reason MEMORY_PRESSURE", code == scheduler.MEMORY_PRESSURE, str(code))

# A game still blocks the release (unchanged)
n = standby_node()
n.state = "busy"
code, _ = scheduler._node_block(n, TIER, 131072, time.time())
check("node busy (game): GPU_BUSY, no release", code == scheduler.GPU_BUSY, str(code))

# The server running (no standby): its model is loaded, the old path applies
n = nodes.Node("strata-box", {"vram_total_gib": 31.8, "upstream_api": "openai", "upstream_releasable": True,
                              "upstream_fallback": True})
n.tunnel = FakeTunnel()
poll.apply_ollama_push(n, {"up": True, "rev": 1, "tags": [{"name": BIG, "digest": f"openai:{BIG}", "size": 0}],
                           "ps": [{"name": BIG, "digest": f"openai:{BIG}", "size_vram": 0}]}, time.time())
n.state, n.hb_ts = "free", time.time()
check("server answering: releases_server_for is False", not n.releases_server_for(BIG))

# A plain Ollama node keeps its budget: a model that does not fit next to the loaded one is still vram_full
p = nodes.Node("gpu-laptop", {"vram_total_gib": 31.8})
poll.apply_ollama_push(p, {"up": True, "rev": 1, "tags": [{"name": DENSE, "digest": "sha256:aa", "size": 17 << 30},
                                                          {"name": "other:30b", "digest": "sha256:cc", "size": 18 << 30}],
                           "ps": [{"name": DENSE, "digest": "sha256:aa", "size_vram": int(25.7 * (1 << 30))}]}, time.time())
p.state, p.hb_ts, p.children_vram_gib, p.foreign_baseline_gib = "free", time.time(), 2.2, 0.7
p.vram_used_gib = 28.6
p.vram_free_gib = 3.2
NEED["other:30b"] = 28.0
state.NODES = {p.name: p}
check("plain Ollama node: releases_server_for is False", not p.releases_server_for("other:30b"))
code, _ = scheduler._node_block(p, {"model": "other:30b", "num_ctx": 32768, "busy_ok": False}, 32768, time.time())
check("plain Ollama node: budget unchanged (loaded models stay counted as evictable, see budget_gib)",
      code == scheduler.VRAM_FULL, str(code))

print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
