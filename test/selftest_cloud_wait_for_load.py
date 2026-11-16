#!/usr/bin/env python3
"""Self-test (Router 0.31.1, ADR-0095): a cloud tier waits once for a local tier before it that is loading.

Trigger 2026-11-16: right after a node switched from an OpenAI-compatible server back to its Ollama, the first request
started loading the role's first model. A request ten seconds later found no local candidate (the load's VRAM claim took
the budget), so the cloud tier further down counted as the first ready one and answered - five seconds before the local
model was ready. Waiting for a loading model existed only for requests without any candidate. Runs without router
process, network, agent or provider."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import cloud, config, nodes, proxy, request, scheduler, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.mqtt = None
state.spawn = lambda coro: coro.close()
LOCAL, CLOUD_MODEL = "example-dense:27b", "synthetic-cloud-small"
state.CFG.models[LOCAL] = {"weights_gib": 17.0, "kv_gib_per_1k": 0.05, "real": True}
LOCAL_TIER = {"model": LOCAL, "num_ctx": 8192, "busy_ok": False}
CLOUD_TIER = {"model": CLOUD_MODEL, "num_ctx": 32768, "busy_ok": False, "cloud": "synthetic"}
state.CLOUD = {"synthetic": cloud.CloudTarget("synthetic", {"kind": "openai", "base_url": "https://provider.invalid/"})}


class OpenRouting(request.Routing):
    """The cloud provider's own guards (key, budget, data class) are not under test here."""

    def tier_blocked(self, tier):
        return None


def loading_node():
    """A node whose router-triggered load of LOCAL is still running: the claim leaves no budget for any local tier."""
    now = time.time()
    n = nodes.Node("gpu-node", {"vram_total_gib": 32.0})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 30, 32.0, 31.0, 1.0
    n.models = {LOCAL}
    n.loading[LOCAL] = (19.0, now + 300)
    return n


def acquire(tiers, deadline_s=60.0):
    req = OpenRouting.fresh()
    req.priority = "interactive"
    role = {"name": "assist", "latency_first": True, "tiers": tiers}
    acq = proxy._Acquire("assist", role, tiers, req, None)
    acq.deadline_at = time.time() + deadline_s
    return acq, role, req


def finish_load(n, after_s):
    async def run():
        await asyncio.sleep(after_s)
        n.loading.pop(LOCAL, None)
        n.loaded[LOCAL] = 19.0
        n.vram_free_gib = 12.0
    return run()


async def main():
    tiers = [LOCAL_TIER, CLOUD_TIER]

    # 0) The instrument: the scheduler alone still picks the cloud tier while the local model is loading
    n = loading_node()
    state.NODES = {n.name: n}
    _, _, cands = scheduler.candidates_for(LOCAL_TIER, None, time.time())
    check("setup: the loading node is no candidate for the local tier (claim takes the budget)", cands == [], str(cands))
    check("setup: loading_for sees the load", scheduler.loading_for([LOCAL_TIER], time.time()) == (n, LOCAL))
    acq, role, req = acquire(tiers)
    pick = scheduler.choose(role, tiers, None, time.time(), (), req)
    check("setup: choose() alone picks the cloud tier (reason cloud)", pick is not None and pick[0] == 1 and req.reason == "cloud",
          f"{pick and pick[0]} {req.reason}")

    # 1) The fix: the request waits for the load and then takes the local tier
    acq, role, req = acquire(tiers)
    t0 = time.time()
    done = asyncio.ensure_future(finish_load(n, 0.6))
    pick = await acq.next_candidate()
    waited = time.time() - t0
    await done
    check("waits for the loading local model instead of going to the cloud", isinstance(pick, tuple) and pick[0] == 0 and pick[3] is n,
          str(pick))
    check("the wait lasted until the load finished (about 0.6 s)", 0.5 <= waited < 3.0, f"{waited:.2f} s")
    check("the wait is counted as queued time", req.queued_ms >= 500, str(req.queued_ms))

    # 2) Only once: a load that does not finish before the deadline ends in the cloud tier, not in a 503 or a loop
    n = loading_node()
    state.NODES = {n.name: n}
    acq, role, req = acquire(tiers, deadline_s=1.0)
    t0 = time.time()
    pick = await acq.next_candidate()
    waited = time.time() - t0
    check("load still running at the deadline: cloud tier after the wait", isinstance(pick, tuple) and pick[0] == 1, str(pick))
    check("waited at most until the deadline", 0.8 <= waited < 2.5, f"{waited:.2f} s")

    # 3) Deadline already over: no wait at all
    n = loading_node()
    state.NODES = {n.name: n}
    acq, role, req = acquire(tiers, deadline_s=-1.0)
    t0 = time.time()
    pick = await acq.next_candidate()
    check("deadline over: cloud tier at once", isinstance(pick, tuple) and pick[0] == 1 and time.time() - t0 < 0.3, str(pick))

    # 4) Nothing loading: the cloud tier is taken at once (the previous behaviour)
    n = loading_node()
    n.loading.clear()
    state.NODES = {n.name: n}
    acq, role, req = acquire(tiers)
    t0 = time.time()
    pick = await acq.next_candidate()
    check("no load running: cloud tier at once", isinstance(pick, tuple) and pick[0] == 1 and time.time() - t0 < 0.3,
          f"{pick} {time.time() - t0:.2f} s")

    # 5) A load for a tier AFTER the cloud tier is no reason to wait (the role ranks the cloud first)
    n = loading_node()
    state.NODES = {n.name: n}
    cloud_first = [CLOUD_TIER, LOCAL_TIER]
    acq, role, req = acquire(cloud_first)
    t0 = time.time()
    pick = await acq.next_candidate()
    check("cloud tier ranked first: no wait for a later local tier", isinstance(pick, tuple) and pick[0] == 0 and time.time() - t0 < 0.3,
          f"{pick} {time.time() - t0:.2f} s")


asyncio.run(main())
print(f"\n{len(FAILS)} FAIL" if FAILS else "\nall PASS")
sys.exit(1 if FAILS else 0)
