#!/usr/bin/env python3
"""Self-test (Router 0.36.0 / Agent 0.36.0, ADR-0102, ADR-0103): a node whose agent runs without a GPU.

The agent reports gpu_present: false in its facts and heartbeats and sends no GPU values. The router then sizes models by
the main memory the agent reports (available RAM + what Ollama holds - reserve, at least the memory-pressure limit), has no
GPU busy detection for it, takes the whole model size as what Ollama holds there, reports no "partly in system RAM"
problem, and accepts the guard state not_applicable without capping the node. A heartbeat without the field (agent
< 0.36.0) keeps the GPU node as before. Game facts: the old wire value "pfad" becomes "path", and enabled: false
(game.enabled) counts as no game detection. Runs without router, network and Ollama (router/config.example.yaml)."""
import os
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, ollamaupdate, poll, problems, registry, scheduler, state   # noqa: E402
from skirnir_router.common import GIB   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.spawn = lambda coro: coro.close()   # no background work here (capability fetch, prewarm)
NEED = {}
state.CFG.need_gib = lambda model, ctx, node=None: NEED[model]
MIN_FREE = state.CFG.memory_pressure["min_free_gib"]
RESERVE_FREE = state.CFG.reserve.get("free", 1.0)
CPU_RESERVE = max(RESERVE_FREE, MIN_FREE)

MEM = {"ram_available_gib": 20.0, "ram_total_gib": 32.0}
CPU_HB = {"gpu_present": False, "memory": MEM, "gpu_guard": {"state": "not_applicable", "source": "-", "problem": False, "reason": "no_gpu"},
          "game": {"supported": False, "enabled": False, "running": False, "folders": 0}}


def cpu_node():
    spec = registry.node_spec_from_entry({"name": "cpu-box", "facts": {"hostname": "CPU-BOX", "gpu_present": False}, "policy": {}})
    n = nodes.Node("cpu-box", spec)
    n.state, n.hb_ts = "free", time.time()
    return n


# --- registration facts and heartbeat
n = cpu_node()
check("facts gpu_present false -> spec and node without GPU", n.gpu_present is False and n.vram_total_gib == 0)
poll.apply_heartbeat(n, dict(CPU_HB), time.time())
check("heartbeat without GPU values is taken: memory known, no GPU utilisation or VRAM", n.memory == MEM and n.gpu_util is None
      and n.vram_used_gib is None and n.vram_free_gib is None, str(n.memory))
check("no GPU busy detection: node stays free, foreign VRAM 0", n.state == "free" and n.foreign_vram_gib() == 0.0)
check("guard state not_applicable is a known state", "not_applicable" in nodes.GUARD_STATES and n.guard_state() == "not_applicable")
state.CFG.gpu_guard = dict(state.CFG.gpu_guard, enabled=True, require_fresh_status=True)
check("not_applicable does not cap the node like throttled", n.effective_max_inflight() == (n.max_inflight or state.CFG.admission["max_inflight_default"]),
      str(n.effective_max_inflight()))
check("game enabled false -> no game known", n.game is None and n.game_since is None)
check("snapshot shows gpu_present false", n.snapshot(time.time())["gpu_present"] is False)

# --- what Ollama holds: the whole model size, no ram_spill
ps = [{"name": "small:8b", "size": int(8 * GIB), "size_vram": 0, "digest": "d1", "context_length": 8192}]
tags = [{"name": "small:8b", "digest": "d1", "size": int(5 * GIB)}, {"name": "mid:14b", "digest": "d2", "size": int(9 * GIB)},
        {"name": "huge:70b", "digest": "d3", "size": int(40 * GIB)}]
for m in ("small:8b", "mid:14b", "huge:70b"):
    state.CAPS[m] = {}
poll.apply_ollama_state(n, tags, ps, time.time(), source="test")
check("loaded model counts with its full size (size, not size_vram 0)", abs(n.loaded.get("small:8b", 0) - 8.0) < 1e-6, str(n.loaded))
check("no ram_spill on a node without a GPU (it always computes in RAM)", n.ram_spill == {}, str(n.ram_spill))

# --- budget and the scheduler's verdict
now = time.time()
want = MEM["ram_available_gib"] + 8.0 - CPU_RESERVE
check("budget = available RAM + what Ollama holds - max(reserve, memory-pressure limit)", abs(n.budget_gib(now, "mid:14b") - want) < 1e-6,
      f"{n.budget_gib(now, 'mid:14b'):.2f} vs {want:.2f}")
check("capacity = reported total RAM", n.capacity_gib() == 32.0)
tier = lambda m: {"model": m, "num_ctx": 8192, "busy_ok": False}   # noqa: E731
NEED.update({"small:8b": 8.5, "mid:14b": 12.0, "huge:70b": 45.0})
code, _ = scheduler._node_block(n, tier("mid:14b"), 8192, now)
check("a model within the RAM budget may load", code is None, str(code))
code, info = scheduler._node_block(n, tier("huge:70b"), 8192, now)
check("a model larger than the main memory is model_too_large", code == scheduler.MODEL_TOO_LARGE, f"{code} {info}")
NEED["mid:14b"] = 32.0 - CPU_RESERVE - 2.0   # fits into the empty memory, not into what is available now
code, info = scheduler._node_block(n, tier("mid:14b"), 8192, now)
check("a model larger than what is available now waits (vram_full, no foreign memory claimed)", code == scheduler.VRAM_FULL, f"{code} {info}")
state.NODES.clear()
state.NODES["cpu-box"] = n
check("role possible when free: capacity from RAM", scheduler.role_possible_when_free({"tiers": [{"model": "mid:14b", "num_ctx": 8192}]}))
check("role not possible when the model exceeds main memory",
      not scheduler.role_possible_when_free({"tiers": [{"model": "huge:70b", "num_ctx": 8192}]}))
state.NODES.clear()

n2 = cpu_node()
n2.hb_ts = 0.0   # no heartbeat yet: no memory known
check("without a heartbeat (memory unknown) nothing fits instead of guessing", n2.budget_gib(time.time(), "small:8b") < 0)

# --- rejected cases: malformed agent values change nothing (telemetry is an assertion of the host, TM-07)
n3 = cpu_node()
poll.apply_heartbeat(n3, dict(CPU_HB, memory={"ram_available_gib": True, "ram_total_gib": "32"}), time.time())
check("non-numeric memory values are not a budget (booleans and strings ignored)", n3.budget_gib(time.time(), "small:8b") < 0
      and n3.capacity_gib() == 0.0, f"{n3.budget_gib(time.time(), 'small:8b')} {n3.capacity_gib()}")
poll.apply_heartbeat(n3, dict(CPU_HB, gpu_present="true"), time.time())
check("gpu_present that is not a boolean leaves the node as it was", n3.gpu_present is False)
spec = registry.node_spec_from_entry({"name": "odd", "facts": {"gpu_present": "no"}, "policy": {}})
check("facts gpu_present other than false keep a GPU node", nodes.Node("odd", spec).gpu_present is True and "gpu_present" not in spec)

# --- agents before 0.36.0 and GPU nodes keep their behaviour
g = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
g.state, g.hb_ts = "free", time.time()
poll.apply_heartbeat(g, {"gpu_util_pct": 3, "vram_total_mib": 32607, "vram_used_mib": 2048, "vram_free_mib": 30559, "memory": MEM}, time.time())
check("heartbeat without gpu_present (agent < 0.36.0) keeps a GPU node", g.gpu_present is True)
check("GPU node budget stays VRAM-based", abs(g.budget_gib(time.time(), "x") - (30559 / 1024 - state.CFG.reserve.get("free", 1.0))) < 1e-6,
      f"{g.budget_gib(time.time(), 'x'):.2f}")
check("GPU node reserve and capacity unchanged", g.reserve_gib("free") == RESERVE_FREE and g.capacity_gib() == 31.8)
poll.apply_ollama_state(g, tags, [{"name": "small:8b", "size": int(8 * GIB), "size_vram": int(6 * GIB), "digest": "d1"}], time.time(), source="test")
check("GPU node: loaded = size_vram, the rest is ram_spill as before", abs(g.loaded["small:8b"] - 6.0) < 1e-6 and abs(g.ram_spill["small:8b"] - 2.0) < 1e-6,
      f"{g.loaded} {g.ram_spill}")
poll.apply_heartbeat(g, {"gpu_present": True, "gpu_util_pct": 3, "vram_total_mib": 32607, "vram_used_mib": 2048, "vram_free_mib": 30559}, time.time())
check("agent >= 0.36.0 with a GPU: gpu_present true", g.gpu_present is True)

# --- game facts: old "pfad", game detection switched off
w = nodes.Node("gamer", {"vram_total_gib": 16})
w.state, w.hb_ts = "free", time.time()
poll.apply_heartbeat(w, {"gpu_util_pct": 1, "vram_total_mib": 16384, "vram_used_mib": 1000, "vram_free_mib": 15384,
                         "game": {"supported": True, "running": True, "name": "Example.exe", "via": "pfad"}}, time.time())
check("agent < 0.36.0: via pfad is read as path", (w.game or {}).get("via") == "path", str(w.game))
poll.apply_heartbeat(w, {"gpu_util_pct": 1, "vram_total_mib": 16384, "vram_used_mib": 1000, "vram_free_mib": 15384,
                         "game": {"supported": True, "enabled": True, "running": True, "name": "Example.exe", "via": "path"}}, time.time())
check("agent >= 0.36.0: via path stays path", (w.game or {}).get("via") == "path" and w.game_since is not None, str(w.game))
poll.apply_heartbeat(w, {"gpu_util_pct": 1, "vram_total_mib": 16384, "vram_used_mib": 1000, "vram_free_mib": 15384,
                         "game": {"supported": True, "enabled": False, "running": False, "folders": 0}}, time.time())
check("game.enabled false: the router forgets the game", w.game is None and w.game_since is None, str(w.game))

# --- structured problems (router 0.35.0, ADR-0101): a node without a GPU raises no GPU problem
state.REG = SimpleNamespace(nodes={}, save=lambda: None)
state.CLOUD = {}
ollamaupdate.LATEST.clear()
state.CFG.gpu_guard = dict(state.CFG.gpu_guard, enabled=True)
now = time.time()
c = cpu_node()
poll.apply_heartbeat(c, dict(CPU_HB), now)
poll.apply_ollama_state(c, tags, ps, now, source="test")
lying = cpu_node()   # an agent that claims a guard problem while reporting no GPU: still nothing to guard
lying.name = "cpu-lying"
lying.guard, lying.guard_ts = {"state": "off", "problem": True, "reason": "deselected"}, now
gpu = nodes.Node("gpu-guarded", {"vram_total_gib": 16})
gpu.state, gpu.hb_ts = "free", now
gpu.guard, gpu.guard_ts = {"state": "off", "problem": True, "reason": "deselected"}, now
state.NODES.clear()
state.NODES.update({x.name: x for x in (c, lying, gpu)})
recs = problems.compute(now, unservable=[])
codes = {(r["code"], r.get("node")) for r in recs}
check("no gpu_guard or ram_spill problem for nodes without a GPU", not any(nd in ("cpu-box", "cpu-lying") and code in ("gpu_guard", "ram_spill")
                                                                         for code, nd in codes), str(sorted(codes)))
check("a GPU node with a guard problem still raises it", ("gpu_guard", "gpu-guarded") in codes, str(sorted(codes)))
state.NODES.clear()

print(f"\n{'ALLE GRUEN' if not FAILS else f'{len(FAILS)} FEHLER: {FAILS}'}")
sys.exit(1 if FAILS else 0)
