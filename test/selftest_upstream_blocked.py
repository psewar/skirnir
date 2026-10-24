#!/usr/bin/env python3
"""Self-test (router 0.17.0 / agent 0.26.0): the agent keeps a held server from starting for want of RAM and says so.

Trigger 2026-10-24: Strata was started at 98 % RAM and a bluescreen followed. Agent 0.26.0 checks the RAM before a
release and reports upstream_blocked (reason, available, total, need, limit) instead of starting. Checks that the router
reads and clears the fact, offers no release candidate while it stands (reason code memory_pressure with the numbers,
not a 180-s wait), stops waiting at once when the block arrives after a release order, and that neither prewarm nor
the window sends releases the agent would refuse. Runs without network, agent or server."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, openai_node, poll, scheduler, state  # noqa: E402

FAILS = []
BIG, DENSE = "big-moe", "dense:27b"
BLOCK = {"reason": "memory", "ram_available_gib": 20.0, "ram_total_gib": 63.4, "need_gib": 42.0, "limit_percent": 90}


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class FakeTunnel:
    def __init__(self):
        self.lock = asyncio.Lock()


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.prewarm_on_online = False
poll.evaluate = lambda node, now: None
state.spawn = lambda coro: coro.close()
ORDERS = []


async def fake_order(node, what, reason):
    ORDERS.append((what, reason))
    node.upstream_order = what
    return True


openai_node.order = fake_order
TIER = {"model": BIG, "num_ctx": 131072, "busy_ok": False}


def push(n, blocked=None):
    msg = {"up": True, "rev": 1, "serving": "ollama", "upstream_state": "held",
           "tags": [{"name": DENSE, "digest": "sha256:aa", "size": 17 << 30}, {"name": BIG, "digest": f"openai:{BIG}", "size": 0}],
           "ps": [{"name": DENSE, "digest": "sha256:aa", "size_vram": 17 << 30}]}
    if blocked is not None:
        msg["upstream_blocked"] = blocked
    poll.apply_ollama_push(n, msg, time.time())


def standby_node(blocked=None):
    n = nodes.Node("strata-box", {"vram_total_gib": 31.8, "upstream_api": "openai", "upstream_releasable": True,
                                  "upstream_fallback": True})
    n.tunnel = FakeTunnel()
    push(n, blocked)
    n.state, n.hb_ts, n.free_at = "free", time.time(), time.time()
    state.NODES = {n.name: n}
    return n


def window(inside):
    lt = time.localtime(time.time() + (-60 if inside else 120) * 60)
    lt2 = time.localtime(time.time() + (60 if inside else 180) * 60)
    state.CFG.upstream_window = {"enabled": True, "window_start": f"{lt.tm_hour:02d}:{lt.tm_min:02d}",
                                 "window_end": f"{lt2.tm_hour:02d}:{lt2.tm_min:02d}", "timezone": "", "idle_hold_s": 900.0}


async def main():
    # 1) The fact: read, shown, cleared
    n = standby_node(BLOCK)
    check("fact read", n.upstream_blocked == BLOCK, str(n.upstream_blocked))
    check("/admin/state shows it", (n.snapshot(time.time()).get("upstream") or {}).get("blocked") == BLOCK)
    push(n)
    check("a frame without the field clears it", n.upstream_blocked is None)
    push(n, "memory")
    check("a malformed value counts as none", n.upstream_blocked is None)

    # 2) No candidate while blocked, the reason with its numbers
    n = standby_node(BLOCK)
    _, _, cands = scheduler.candidates_for(TIER, None, time.time())
    check("blocked: no release candidate", cands == [], str([c.name for c in cands]))
    code, info = scheduler._node_block(n, TIER, 131072, time.time())
    check("reason code memory_pressure from the agent", code == scheduler.MEMORY_PRESSURE and info.get("need_gib") == 42.0, f"{code} {info}")
    why = scheduler.unavailable([TIER], None, time.time())
    check("refusal names the numbers and that the agent holds it",
          why["code"] == scheduler.MEMORY_PRESSURE and "needs 42.0 GiB" in why["detail"] and "20.0 of 63.4" in why["detail"]
          and "agent keeps it held" in why["detail"] and why["retry_after_s"] == 60, why["detail"])
    push(n)
    _, _, cands = scheduler.candidates_for(TIER, None, time.time())
    check("block cleared: candidate again", cands == [n])

    # 3) A release order the agent refuses: the wait ends at once, not after 180 s
    n = standby_node()

    async def refuse(node, what, reason):
        ORDERS.append((what, reason))
        push(node, BLOCK)   # the agent's next frame
        return True
    openai_node.order = refuse
    t = time.time()
    ok = await openai_node.ensure_loaded(n, BIG, timeout_s=30)
    check("refused release: ensure_loaded False within seconds", not ok and time.time() - t < 5, f"{time.time() - t:.1f} s")
    openai_node.order = fake_order

    # 4) Prewarm and the window send no release the agent would refuse
    window(inside=True)
    n = standby_node(BLOCK)
    ORDERS.clear()
    await poll.prewarm(n, 0, "free")
    check("prewarm inside the window: no release while blocked", not any(o[0] == "release" for o in ORDERS), str(ORDERS))
    ORDERS.clear()
    state.spawn = lambda coro: asyncio.get_event_loop().create_task(coro)
    poll.upstream_window_check(n, time.time())
    await asyncio.sleep(0)
    check("window: no release while blocked", ORDERS == [], str(ORDERS))
    push(n)
    n.window_order_at = 0.0
    poll.upstream_window_check(n, time.time())
    await asyncio.sleep(0)
    check("window: release once the block is gone", ORDERS == [("release", "night window")], str(ORDERS))
    state.spawn = lambda coro: coro.close()


async def no_poll(node):   # prewarm polls the node first; there is no agent here
    return None


poll.poll_node = no_poll
asyncio.run(main())
print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
