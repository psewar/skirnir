#!/usr/bin/env python3
"""Self-test (router 0.12.0 / agent 0.23.0): the standby Ollama of an OpenAI-compatible node.

While the server is held for a game, the agent runs an Ollama in its place and reports serving "ollama": its models,
plus the held server's (digest "openai:<id>"). Checks that the router then treats the node as an Ollama node (busy_ok
model selectable during busy, Ollama unload for anything else, /api/show and caps for the standby's models), sends only
the server's model the OpenAI way (which releases it), and that prewarm releases the server instead of loading anything
into the standby. Runs without network, agent or server."""
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, poll, registry, scheduler, state  # noqa: E402

FAILS = []
BIG = "big-moe"
SMALL = "small:8b"
OTHER = "medium:27b"


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


SERVER_TAG = {"name": BIG, "digest": f"openai:{BIG}", "size": 0}
STANDBY_TAGS = [{"name": SMALL, "digest": "sha256:aa", "size": 5 << 30}, {"name": OTHER, "digest": "sha256:bb", "size": 17 << 30}]


def push(n, tags, ps, upstream_state=None, serving=None):
    msg = {"up": True, "rev": 1, "tags": tags, "ps": ps}
    if upstream_state:
        msg["upstream_state"] = upstream_state
    if serving:
        msg["serving"] = serving
    poll.apply_ollama_push(n, msg, time.time())


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def main():
    state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
    state.CFG.prewarm_on_online = False
    state.CFG.unload_on_busy = True
    state.CFG.unload_on_busy_interval_s = 0
    state.CFG.busy_ok_models = {SMALL}
    poll.evaluate = lambda node, now: None   # the busy/free state machine is set by hand here
    fetched = []

    async def fake_fetch_caps(node, m):
        fetched.append(m)
    poll.fetch_caps = fake_fetch_caps

    # --- registry: the agent fact reaches the node ---
    facts = {"upstream_api": "openai", "upstream_release": True, "upstream_fallback": True}
    spec = registry.node_spec_from_entry({"name": "strata-box", "facts": facts, "policy": {}})
    check("agent fact upstream_fallback -> node spec", spec.get("upstream_fallback") is True, str(spec))
    no_release = registry.node_spec_from_entry({"name": "x", "facts": {"upstream_api": "openai", "upstream_fallback": True}, "policy": {}})
    check("standby without upstream_release is ignored (nothing to stand in for)", "upstream_fallback" not in no_release)
    n = nodes.Node("strata-box", {**spec, "vram_total_gib": 32})
    n.tunnel = FakeTunnel()
    check("node knows its standby", n.upstream_fallback and n.openai_upstream)

    # --- server running: OpenAI node as before ---
    push(n, [SERVER_TAG], [{"name": BIG, "digest": f"openai:{BIG}", "size_vram": 0}])
    check("server answering: speaks OpenAI, its model goes the OpenAI way", n.speaks_openai and n.serves_openai(BIG))

    # --- held, standby serving ---
    push(n, [SERVER_TAG], [], upstream_state="held")
    check("held, standby not yet answering: still the OpenAI node (plain hold)", n.speaks_openai and n.upstream_serving == "")
    fetched.clear()
    push(n, STANDBY_TAGS + [SERVER_TAG], [], upstream_state="held", serving="ollama")
    await settle()
    check("standby serving: node is an Ollama node", not n.speaks_openai and n.upstream_serving == "ollama")
    check("standby's models go the Ollama way, the held server's the OpenAI way",
          not n.serves_openai(SMALL) and not n.serves_openai(OTHER) and n.serves_openai(BIG))
    check("all models known (server's still listed, so a request can release it)", n.models == {SMALL, OTHER, BIG}, str(n.models))
    check("capabilities fetched for the standby's models only (/api/show), not for the server's",
          BIG not in fetched and SMALL in fetched, str(fetched))
    snap = n.snapshot(time.time())
    check("/admin/state shows the standby", (snap.get("upstream") or {}).get("serving") == "ollama"
          and snap["upstream"].get("fallback") is True, str(snap.get("upstream")))

    # --- busy: the busy_ok model is selectable, others are not, and Ollama unload applies ---
    n.state = "busy"
    tier_small = {"model": SMALL, "num_ctx": 8192, "busy_ok": True}
    tier_big = {"model": BIG, "num_ctx": 131072, "busy_ok": False}
    small_code, _ = scheduler._node_block(n, tier_small, 8192, time.time())
    big_code, _ = scheduler._node_block(n, tier_big, 131072, time.time())
    check("busy: busy_ok model of the standby is not blocked by the game", small_code != scheduler.GPU_BUSY, str(small_code))
    check("busy: the held server's model is blocked (no release during a game)", big_code == scheduler.GPU_BUSY, str(big_code))

    unloaded = []

    class Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return b"{}"

    def fake_nreq(node, method, path, **kw):
        unloaded.append((path, (kw.get("json") or {}).get("model")))
        return Resp()
    orig_nreq = nodes.nreq
    nodes.nreq = fake_nreq
    push(n, STANDBY_TAGS + [SERVER_TAG], [{"name": SMALL, "digest": "sha256:aa", "size_vram": 5 << 30},
                                          {"name": OTHER, "digest": "sha256:bb", "size_vram": 17 << 30}],
         upstream_state="held", serving="ollama")
    n.tunnel.ws.sent.clear()
    await poll.unload_big_models(n)
    nodes.nreq = orig_nreq
    check("busy with the standby: non-busy_ok model unloaded through Ollama, busy_ok model stays, no hold order",
          unloaded == [("/api/generate", OTHER)] and n.tunnel.ws.sent == [], f"{unloaded} {n.tunnel.ws.sent}")

    # --- free again: prewarm releases the server, loads nothing into the standby ---
    push(n, STANDBY_TAGS + [SERVER_TAG], [{"name": SMALL, "digest": "sha256:aa", "size_vram": 5 << 30}],
         upstream_state="held", serving="ollama")
    n.state, n.hb_ts, n.foreign_since, n.free_at = "free", time.time(), None, time.time()
    n.tunnel.ws.sent.clear()
    await poll.prewarm(n, 0, "free", since=n.free_at)
    check("free again with the standby serving -> prewarm sends upstream-release only",
          n.tunnel.ws.sent == [{"t": "upstream-release"}], str(n.tunnel.ws.sent))
    push(n, [SERVER_TAG], [], upstream_state="starting")
    check("agent reports the release: standby gone, the OpenAI node again", n.speaks_openai and n.models == {BIG}
          and n.upstream_serving == "")

    # --- a node without the fact: a stray serving field does not switch anything off the OpenAI path for its model ---
    k = nodes.Node("plain", {"upstream_api": "openai", "upstream_releasable": True, "vram_total_gib": 32})
    push(k, [SERVER_TAG], [{"name": BIG, "digest": f"openai:{BIG}", "size_vram": 0}])
    check("server model always OpenAI on a plain OpenAI node", k.serves_openai(BIG))
    o = nodes.Node("ollama-box", {"vram_total_gib": 16})
    push(o, STANDBY_TAGS, [])
    check("plain Ollama node: nothing goes the OpenAI way", not o.speaks_openai and not o.serves_openai(SMALL))


asyncio.run(main())
print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
