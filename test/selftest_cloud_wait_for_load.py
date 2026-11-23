#!/usr/bin/env python3
"""Self-test (Router 0.31.1 ADR-0095, 0.32.0 ADR-0096): a cold pick waits once for a tier before it that is loading.

Trigger 2026-11-16: right after a node switched from an OpenAI-compatible server back to its Ollama, the first request
started loading the role's first model. A request ten seconds later found no local candidate (the load's VRAM claim took
the budget), so the cloud tier further down counted as the first ready one and answered - five seconds before the local
model was ready (fixed in 0.31.1). The re-measurement showed the same with a small local tier: it fitted next to the claim,
was loaded cold as a second model (7.6 GiB) for one tool round, and the answer round still waited for the first model
(0.32.0). Waiting for a loading model existed only for requests without any candidate. Runs without router process,
network, agent or provider."""
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
LOCAL, SMALL, OTHER, CLOUD_MODEL = "example-dense:27b", "example-small:8b", "example-other:12b", "synthetic-cloud-small"
state.CFG.models[LOCAL] = {"weights_gib": 17.0, "kv_gib_per_1k": 0.05, "real": True}
state.CFG.models[SMALL] = {"weights_gib": 5.0, "kv_gib_per_1k": 0.01, "real": True}
LOCAL_TIER = {"model": LOCAL, "num_ctx": 8192, "busy_ok": False}
SMALL_TIER = {"model": SMALL, "num_ctx": 8192, "busy_ok": False}
CLOUD_TIER = {"model": CLOUD_MODEL, "num_ctx": 32768, "busy_ok": False, "cloud": "synthetic"}
state.CLOUD = {"synthetic": cloud.CloudTarget("synthetic", {"kind": "openai", "base_url": "https://provider.invalid/"})}


class OpenRouting(request.Routing):
    """The cloud provider's own guards (key, budget, data class) are not under test here."""

    def tier_blocked(self, tier):
        return None


def loading_node(free_gib=1.0, loading=LOCAL):
    """A node whose router-triggered load of `loading` is still running; with 1 GiB free no local tier fits beside the
    claim, with 9 GiB the small tier does (as on 2026-11-16 11:21)."""
    now = time.time()
    n = nodes.Node("gpu-node", {"vram_total_gib": 32.0})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 30, 32.0, 32.0 - free_gib, free_gib
    n.models = {LOCAL, SMALL, OTHER}
    n.loading[loading] = (19.0, now + 300)
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


async def picked_at_once(acq):
    t0 = time.time()
    pick = await acq.next_candidate()
    return pick, time.time() - t0


async def main():
    tiers = [LOCAL_TIER, CLOUD_TIER]

    # 0) The instrument: the scheduler alone still picks the cloud tier while the local model is loading
    n = loading_node()
    state.NODES = {n.name: n}
    _, cands = scheduler.candidates_for(LOCAL_TIER, None, time.time())
    check("setup: the loading node is no candidate for the local tier (claim takes the budget)", cands == [], str(cands))
    check("setup: tier_loading sees the load of the tier's own model", scheduler.tier_loading([LOCAL_TIER], time.time()) == (n, LOCAL))
    acq, role, req = acquire(tiers)
    pick = scheduler.choose(role, tiers, None, time.time(), (), req)
    check("setup: choose() alone picks the cloud tier (reason cloud)", pick is not None and pick[0] == 1 and req.reason == "cloud",
          f"{pick and pick[0]} {req.reason}")

    # 1) ADR-0095: the request waits for the load and then takes the local tier
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
    pick, s = await picked_at_once(acquire(tiers, deadline_s=-1.0)[0])
    check("deadline over: cloud tier at once", isinstance(pick, tuple) and pick[0] == 1 and s < 0.3, str(pick))

    # 4) Nothing loading: the cloud tier is taken at once (the previous behaviour)
    n = loading_node()
    n.loading.clear()
    state.NODES = {n.name: n}
    pick, s = await picked_at_once(acquire(tiers)[0])
    check("no load running: cloud tier at once", isinstance(pick, tuple) and pick[0] == 1 and s < 0.3, f"{pick} {s:.2f} s")

    # 5) A load for a tier AFTER the cloud tier is no reason to wait (the role ranks the cloud first)
    n = loading_node()
    state.NODES = {n.name: n}
    pick, s = await picked_at_once(acquire([CLOUD_TIER, LOCAL_TIER])[0])
    check("cloud tier ranked first: no wait for a later local tier", isinstance(pick, tuple) and pick[0] == 0 and s < 0.3,
          f"{pick} {s:.2f} s")

    # 6) ADR-0096: a lower local tier that would be loaded cold beside the claim waits for the higher tier's load
    small_tiers = [LOCAL_TIER, SMALL_TIER, CLOUD_TIER]
    n = loading_node(free_gib=9.0)
    state.NODES = {n.name: n}
    _, cands = scheduler.candidates_for(SMALL_TIER, None, time.time())
    check("setup: the small tier fits beside the claim (cold candidate)", cands == [n], str(cands))
    acq, role, req = acquire(small_tiers)
    pick = scheduler.choose(role, small_tiers, None, time.time(), (), req)
    check("setup: choose() alone picks the small tier cold", pick is not None and pick[0] == 1 and not n.is_loaded(SMALL), str(pick))
    acq, role, req = acquire(small_tiers)
    t0 = time.time()
    done = asyncio.ensure_future(finish_load(n, 0.6))
    pick = await acq.next_candidate()
    waited = time.time() - t0
    await done
    check("a cold lower tier waits for the loading first tier and then takes it", isinstance(pick, tuple) and pick[0] == 0,
          str(pick))
    check("that wait lasted until the load finished", 0.5 <= waited < 3.0, f"{waited:.2f} s")

    # 7) A WARM lower tier is still taken at once (no second model is loaded; the warm-first rule stands)
    n = loading_node(free_gib=9.0)
    n.loaded[SMALL] = 5.1
    state.NODES = {n.name: n}
    pick, s = await picked_at_once(acquire(small_tiers)[0])
    check("warm lower tier: taken at once while the first tier loads", isinstance(pick, tuple) and pick[0] == 1 and s < 0.3,
          f"{pick} {s:.2f} s")

    # 8) A load of a model that is no tier of the role is no reason to wait (it will not serve this request)
    n = loading_node(free_gib=9.0, loading=OTHER)
    state.NODES = {n.name: n}
    check("setup: tier_loading ignores a load of another model", scheduler.tier_loading([LOCAL_TIER], time.time()) is None)
    pick, s = await picked_at_once(acquire([LOCAL_TIER, SMALL_TIER])[0])
    check("another model loading: no wait (first tier taken cold or the small one, at once)", isinstance(pick, tuple) and s < 0.3,
          f"{pick} {s:.2f} s")


asyncio.run(main())
print(f"\n{len(FAILS)} FAIL" if FAILS else "\nall PASS")
sys.exit(1 if FAILS else 0)
