#!/usr/bin/env python3
"""Self-test (Router 0.16.0): an OpenAI-compatible server (Strata) only in a window or on request (modes.upstream_window).

Trigger 2026-10-23: Strata takes most of the card; the operator wants it overnight (Hermes programming) and on explicit
request, and the standby Ollama during the day. Checks the hold outside the window after idle, the release inside it,
no ping-pong after a standby request at night, prewarm and residency outside the window, and that nothing changes
with the window disabled. Runs without router process, network or agent."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, openai_node, poll, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.mqtt = None
STRATA, OLLAMA_MODEL = "synthetic-moe-flash", "example-dense:27b"
ORDERS = []


async def fake_order(node, what, reason):
    ORDERS.append((what, reason))
    node.upstream_order = what
    return True


openai_node.order = fake_order


async def no_poll(node):   # prewarm polls the node first; there is no agent here
    return None


poll.poll_node = no_poll
state.spawn = lambda coro: asyncio.run(coro)


def hhmm(offset_min):
    lt = time.localtime(time.time() + offset_min * 60)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}"


def window(inside, enabled=True):
    """A window around now (inside) or one that starts in two hours (outside); router local time."""
    start, end = (hhmm(-60), hhmm(60)) if inside else (hhmm(120), hhmm(180))
    state.CFG.upstream_window = {"enabled": enabled, "window_start": start, "window_end": end, "timezone": "", "idle_hold_s": 900.0}


class FakeTunnel:
    pass


def strata_node(serving_standby=False):
    now = time.time()
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "upstream_api": "openai", "upstream_releasable": True,
                                    "upstream_fallback": True})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.tunnel = FakeTunnel()
    n.digest_of = {STRATA: "openai:" + STRATA}
    if serving_standby:
        n.upstream_serving, n.upstream_order = openai_node.SERVING_OLLAMA, "hold"
        n.models, n.loaded = {STRATA, OLLAMA_MODEL}, {}
    else:
        n.models, n.loaded = {STRATA}, {STRATA: 0.0}
    state.NODES = {n.name: n}
    return n


def run_check(n):
    ORDERS.clear()
    poll.upstream_window_check(n, time.time())
    return list(ORDERS)


# 1) Outside the window: an idle server is held, a recently used one keeps running
window(inside=False)
n = strata_node()
check("outside, server never used since start -> hold", run_check(n) == [("hold", "outside the night window")])
n = strata_node()
n.last_used[STRATA] = time.time() - 60
check("outside, server used a minute ago (a request started it) -> no hold yet", run_check(n) == [])
n.last_used[STRATA] = time.time() - 901
check("outside, idle longer than idle_hold_s -> hold", run_check(n) == [("hold", "outside the night window")])
n = strata_node()
n.inflight = 1
check("outside, a request is running -> no hold", run_check(n) == [])
n = strata_node()
n.upstream_state = openai_node.STARTING
check("outside, server still starting (a request released it) -> no hold", run_check(n) == [])
n = strata_node(serving_standby=True)
check("outside, standby already serves -> nothing to do", run_check(n) == [])

# 2) Inside the window: release a held server once the node is free and the standby idle
window(inside=True)
n = strata_node(serving_standby=True)
check("inside, standby serves and was not used -> release", run_check(n) == [("release", "night window")])
n = strata_node(serving_standby=True)
n.last_used[OLLAMA_MODEL] = time.time() - 30
check("inside, standby model used 30 s ago (deliberate switch) -> no release yet (no ping-pong)", run_check(n) == [])
n = strata_node(serving_standby=True)
n.state = "busy"
check("inside, node busy (game) -> stays held", run_check(n) == [])
n = strata_node()
check("inside, server already runs -> nothing", run_check(n) == [])
# 0.16.3: after a router restart the router no longer remembers its hold; the agent's report decides
n = strata_node(serving_standby=True)
n.upstream_order, n.upstream_state = "", openai_node.HELD
check("inside, after a router restart (no remembered order, agent reports held) -> release",
      run_check(n) == [("release", "night window")])
n = strata_node()
n.upstream_order = "hold"   # stale memory: the server answers again (released by a request)
check("inside, remembered hold but the server answers -> nothing", run_check(n) == [])

# 3) One order at a time
window(inside=False)
n = strata_node()
first = run_check(n)
second = run_check(n)
check("a second check right after an order sends nothing (gap)", first and second == [])

# 4) Prewarm and residency outside the window must not start the server
window(inside=False)
n = strata_node(serving_standby=True)
ORDERS.clear()
asyncio.run(poll.prewarm(n, 0, "free"))
check("prewarm outside the window: no release", not any(o[0] == "release" for o in ORDERS), str(ORDERS))
window(inside=True)
n = strata_node(serving_standby=True)
n.upstream_state = openai_node.HELD
ORDERS.clear()
asyncio.run(poll.prewarm(n, 0, "free"))
check("prewarm inside the window releases as before", ORDERS == [("release", "prewarm free")], str(ORDERS))
check("server_allowed: inside True", openai_node.server_allowed(n, time.time()))
window(inside=False)
check("server_allowed: outside False", not openai_node.server_allowed(n, time.time()))

# 5) Window disabled: old behaviour, the server may always run
window(inside=False, enabled=False)
n = strata_node()
check("disabled: no hold even when idle", run_check(n) == [])
check("disabled: server_allowed True", openai_node.server_allowed(n, time.time()))
plain = nodes.Node("gpu-laptop", {"vram_total_gib": 16.0})
window(inside=False)
check("an Ollama-only node is never affected", openai_node.server_allowed(plain, time.time()))

# 6) 0.16.1: after a window hold the standby is prewarmed once (it starts empty; prewarm on free/online and residency
#    do not fire then, so the first request of the day loaded cold)
PREWARMS = []
real_prewarm = poll.prewarm


async def fake_prewarm(node, delay, reason, since=None):
    PREWARMS.append(reason)


poll.prewarm = fake_prewarm
window(inside=False)
n = strata_node(serving_standby=True)
PREWARMS.clear()
run_check(n)
run_check(n)
check("outside, standby serves empty -> prewarm once, not every tick", PREWARMS == ["standby"], str(PREWARMS))
n = strata_node(serving_standby=True)
n.loaded = {OLLAMA_MODEL: 0.0}
PREWARMS.clear()
run_check(n)
check("outside, a standby model is already loaded -> no prewarm", PREWARMS == [], str(PREWARMS))
n = strata_node(serving_standby=True)
n.loading = {OLLAMA_MODEL: (18.0, time.time() + 300)}
PREWARMS.clear()
run_check(n)
check("outside, a request is loading a model (switch on request) -> no prewarm", PREWARMS == [], str(PREWARMS))
n = strata_node(serving_standby=True)
n.state = "busy"
PREWARMS.clear()
run_check(n)
check("outside, node busy (game hold) -> no prewarm", PREWARMS == [], str(PREWARMS))
window(inside=False, enabled=False)
n = strata_node(serving_standby=True)
PREWARMS.clear()
run_check(n)
check("window disabled -> no standby prewarm from the window check", PREWARMS == [], str(PREWARMS))
poll.prewarm = real_prewarm

print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
