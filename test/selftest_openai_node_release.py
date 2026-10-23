#!/usr/bin/env python3
"""Self-test (router 0.11.0 / agent 0.22.0): freeing the GPU from an OpenAI-compatible node.

An OpenAI-compatible server cannot unload its model. When the node turns busy (a game), the router orders the agent to
stop the server ({"t":"upstream-hold"}); when the node is free again, prewarm orders a start ({"t":"upstream-release"});
a request for a stopped server's model starts it and waits until the agent reports it loaded. Checks against a fake
tunnel that records the orders. Runs without network, agent or server."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, openai_node, poll, state  # noqa: E402

FAILS = []
MODEL = "big-moe"


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


class FakeTunnel:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.ws = FakeWS()

    def name(self):
        return "fake"


def node(releasable=True):
    n = nodes.Node("strata-box", {"upstream_api": "openai", "upstream_releasable": releasable, "vram_total_gib": 32})
    n.tunnel = FakeTunnel()
    return n


def push(n, ps, upstream_state=None, now=None):
    msg = {"up": True, "rev": 1, "tags": [{"name": MODEL, "digest": f"openai:{MODEL}"}], "ps": ps}
    if upstream_state:
        msg["upstream_state"] = upstream_state
    poll.apply_ollama_push(n, msg, now or time.time())


LOADED = [{"name": MODEL, "digest": f"openai:{MODEL}", "size_vram": 0, "context_length": 131072}]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def main():
    state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
    state.CFG.prewarm_on_online = False
    state.CFG.unload_on_busy = True
    state.CFG.unload_on_busy_interval_s = 0
    poll.evaluate = lambda node, now: None   # the busy/free state machine is set by hand here

    # --- busy -> hold ---
    n = node()
    push(n, LOADED)
    check("agent report: model loaded, no upstream state", n.is_loaded(MODEL) and n.upstream_state == "")
    n.state = "busy"
    poll.busy_unload_check(n, time.time())
    await settle()
    check("busy with the model loaded -> upstream-hold sent (size 0 does not hide it)",
          n.tunnel.ws.sent == [{"t": "upstream-hold"}] and n.upstream_order == "hold", str(n.tunnel.ws.sent))

    push(n, [], upstream_state="held")
    check("agent reports held: up, nothing loaded, state held", n.upstream_state == "held" and not n.loaded and n.state != "offline")
    n.tunnel.ws.sent.clear()
    poll.busy_unload_check(n, time.time())
    await settle()
    check("held and nothing loaded -> no further order", n.tunnel.ws.sent == [])

    # --- not while a request runs, not for busy_ok models, not without the agent fact ---
    m = node()
    push(m, LOADED)
    m.state = "busy"
    m.inflight_models[MODEL] = 1
    await poll.unload_big_models(m)
    check("request running on the server -> no hold yet", m.tunnel.ws.sent == [])
    m.inflight_models[MODEL] = 0
    state.CFG.busy_ok_models.add(MODEL)
    await poll.unload_big_models(m)
    check("busy_ok model may stay during a game -> no hold", m.tunnel.ws.sent == [])
    state.CFG.busy_ok_models.discard(MODEL)
    k = node(releasable=False)
    push(k, LOADED)
    k.state = "busy"
    poll.busy_unload_check(k, time.time())
    await settle()
    check("agent without upstream_release -> no order (node just skipped while busy)", k.tunnel.ws.sent == [])

    # --- free again -> prewarm releases ---
    n.state, n.hb_ts, n.foreign_since, n.free_at = "free", time.time(), None, time.time()
    n.tunnel.ws.sent.clear()
    await poll.prewarm(n, 0, "free", since=n.free_at)
    check("free again after a hold -> prewarm sends upstream-release", n.tunnel.ws.sent == [{"t": "upstream-release"}], str(n.tunnel.ws.sent))
    push(n, [], upstream_state="starting")
    check("agent reports starting: still up, nothing loaded", n.upstream_state == "starting" and n.state == "free" and not n.loaded)
    push(n, LOADED)
    check("server answers again: loaded, no upstream state", n.is_loaded(MODEL) and n.upstream_state == "")
    n.tunnel.ws.sent.clear()
    await poll.prewarm(n, 0, "free", since=n.free_at)
    check("prewarm with the server running -> nothing to do", n.tunnel.ws.sent == [])

    n.game_since, n.game_heavy = time.time(), True
    push(n, [], upstream_state="held")
    await poll.prewarm(n, 0, "free", since=n.free_at)
    check("held while a heavy game runs -> no release", n.tunnel.ws.sent == [])
    n.game_since, n.game_heavy = None, False

    # --- a request needs the model of a held server ---
    r = node()
    push(r, [], upstream_state="held")
    r.state = "free"

    async def agent_loads():
        await asyncio.sleep(0.2)
        push(r, LOADED)
    task = asyncio.ensure_future(agent_loads())
    ok = await openai_node.ensure_loaded(r, MODEL, timeout_s=5)
    await task
    check("request on a held server: release sent, waits until loaded", ok and r.tunnel.ws.sent == [{"t": "upstream-release"}], str(r.tunnel.ws.sent))

    s = node()
    push(s, [], upstream_state="starting")
    s.state = "free"
    t0 = time.time()
    ok = await openai_node.ensure_loaded(s, MODEL, timeout_s=1)
    check("starting server that does not load in time -> False after the wait, no extra order (already starting)",
          not ok and s.tunnel.ws.sent == [] and time.time() - t0 >= 1)
    u = node(releasable=False)
    push(u, [], upstream_state="held")
    check("cannot be started by the router -> False at once", await openai_node.ensure_loaded(u, MODEL, timeout_s=5) is False)

    snap = n.snapshot(time.time())
    check("/admin/state shows the upstream (api, releasable, state, last order)",
          snap.get("upstream", {}).get("releasable") is True and snap["upstream"]["state"] in ("held", "starting", "answering"), str(snap.get("upstream")))


asyncio.run(main())
print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
