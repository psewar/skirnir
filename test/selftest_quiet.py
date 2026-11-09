#!/usr/bin/env python3
"""Self-test (Router 0.21.0, ADR-0061): quiet for a measurement, per node.

Trigger 2026-11-09: a test bench measured models directly through Ollama. The router took the measured model for foreign
memory, turned the node busy, unloaded the model and prewarmed the rank-1 model again: 6 of 9 runs were void. The
workaround switched global settings (unload_on_busy, prewarm) for every node. Quiet does it for one node, ends by itself,
and refuses to end early while the safety net would then unload the measured model.
Runs without router, network or Ollama (real config from router/config.example.yaml); state.spawn is recorded."""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, config, nodes, poll, quiet, registry, state   # noqa: E402
from skirnir_router.outcome import failed   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
PLANNED = []


def record(coro):
    """state.spawn stub: records which coroutine would run with which reason, and closes it."""
    f = getattr(coro, "cr_frame", None)
    PLANNED.append((getattr(coro, "__name__", "?"), (f.f_locals.get("reason") if f else None)))
    coro.close()


state.spawn = record
NREQ = []


def no_network(*a, **kw):
    NREQ.append(a)
    raise AssertionError("no network in this self-test")   # exception-ok: test stub, a call is the failure itself


nodes.nreq = no_network
RANK1 = "qwen3.6:35b-a3b"
BENCH = "bench-model:1b"
FP = "f" * 64


def add_node():
    now = time.time()
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 80.0, "foreign_vram_baseline_gib": 0.0})
    n.fp = FP
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 2, 80.0, 1.0, 79.0
    n.models = {RANK1, BENCH}
    state.NODES = {n.name: n}
    state.REG = SimpleNamespace(nodes={FP: {"name": n.name, "state": "approved"}}, save=lambda: None, dirty=False)
    return n


def names(kind):
    return [r for name, r in PLANNED if name == kind]


now = time.time()

# 1) Request validation: facts, not exceptions
ok = quiet.parse_start({"until": now + 600, "reason": "  tempo\x1b[31m run\n "}, now)
check("valid request -> (until, reason), control characters and spaces folded", not failed(ok) and ok[1] == "tempo [31m run",
      str(ok))
for body, why in [({"until": True, "reason": "x"}, "bool"), ({"until": "soon", "reason": "x"}, "text"),
                  ({"until": float("inf"), "reason": "x"}, "infinite"), ({"until": now - 1, "reason": "x"}, "past"),
                  ({"until": now + quiet.MAX_QUIET_S + 60, "reason": "x"}, "beyond the maximum"),
                  ({"until": now + 60, "reason": " \t"}, "empty reason"), ({"until": now + 60}, "no reason")]:
    p = quiet.parse_start(body, now)
    check(f"until/reason {why} -> 400", failed(p) and p.status == 400, str(p))

# 2) Target node by fingerprint
n = add_node()
check("approved, active fingerprint -> node", quiet.target(FP) is n)
check("unknown fingerprint -> 404", getattr(quiet.target("0" * 64), "status", None) == 404)
state.REG.nodes[FP]["state"] = "pending"
check("pending fingerprint -> 409", getattr(quiet.target(FP), "status", None) == 409)
state.REG.nodes[FP]["state"] = "approved"

# 3) Busy safety net: control without quiet, then with quiet
n = add_node()
n.state, n.busy_reason, n.loaded = "busy", "foreign_vram", {BENCH: 6.0}
PLANNED.clear()
poll.busy_unload_check(n, time.time())
check("control: busy with the measured model loaded -> safety net unloads", names("unload_big_models") == [None], str(PLANNED))
n = add_node()
n.state, n.busy_reason, n.loaded = "busy", "foreign_vram", {BENCH: 6.0}
quiet.start(n, time.time() + 600, "tempo run", time.time())
PLANNED.clear()
t = time.time()
poll.busy_unload_check(n, t)
check("quiet: safety net unloads nothing", names("unload_big_models") == [], str(PLANNED))
check("... and notes the skip once per interval", n.last_busy_unload == t)
check("snapshot shows the quiet with reason and until", (n.snapshot(t)["quiet"] or {}).get("reason") == "tempo run"
      and n.snapshot(t)["quiet"]["until"] == n.quiet_until, str(n.snapshot(t)["quiet"]))

# 4) Busy detection stays on, but the busy transition unloads nothing
n = add_node()
n.loaded = {BENCH: 6.0}
n.vram_used_gib = 30.0   # far more than Ollama reports: foreign memory
n.foreign_since = time.time() - state.CFG.foreign_sustain - 5
quiet.start(n, time.time() + 600, "tempo run", time.time())
PLANNED.clear()
poll.evaluate(n, time.time())
check("quiet: foreign memory still turns the node busy (real traffic keeps away)", n.state == "busy", n.state)
check("... without an unload on the transition", names("unload_big_models") == [], str(PLANNED))

# 5) Prewarm is skipped before any network or wait
n = add_node()
quiet.start(n, time.time() + 600, "tempo run", time.time())
NREQ.clear()
t0 = time.time()
asyncio.run(poll.prewarm(n, 0, "free", since=n.free_at))
check("quiet: prewarm returns at once without touching the node", NREQ == [] and time.time() - t0 < 1.0,
      f"{NREQ} {time.time() - t0:.2f}s")


# 6) Residency rule: control without quiet, then with quiet
def residency_node():
    n = add_node()
    n.loaded = {BENCH: 6.0}
    n.free_since = time.time() - state.CFG.prewarm_free_delay - 5
    return n


n = residency_node()
PLANNED.clear()
poll.residency_check(n, time.time())
check("control: measured model displaces rank 1 -> residency prewarm", names("prewarm") == ["residency"], str(PLANNED))
n = residency_node()
quiet.start(n, time.time() + 600, "tempo run", time.time())
PLANNED.clear()
poll.residency_check(n, time.time())
check("quiet: no residency prewarm", names("prewarm") == [], str(PLANNED))

# 7) Extending keeps since; expiry ends by itself and prewarms a free node once
n = add_node()
t = time.time()
quiet.start(n, t + 60, "first", t)
since = n.quiet_since
quiet.start(n, t + 900, "second", t + 30)
check("extending keeps since, takes the new until and reason", n.quiet_since == since and n.quiet_until == t + 900
      and n.quiet_reason == "second")
n.quiet_until = time.time() - 1
PLANNED.clear()
before = len(state.DECISIONS)
quiet.expire(n, time.time())
ended = [e for e in state.DECISIONS[before:] if e.get("event") == "quiet_end"]
check("until passed -> quiet ends by itself", n.quiet_until == 0.0 and n.snapshot(time.time())["quiet"] is None)
check("... journals the end with why=expired", len(ended) == 1 and ended[0]["why"] == "expired", str(ended))
check("... and prewarms the free node once", names("prewarm") == ["quiet_end"], str(PLANNED))
PLANNED.clear()
quiet.expire(n, time.time())
check("a second tick does nothing", names("prewarm") == [] and len(state.DECISIONS) == before + 1)


# 8) HTTP: start, refused end while busy, accepted end, idempotent end
async def http():
    ctl = web.Application()
    ctl.router.add_post("/admin/nodes/{fp}/quiet", quiet.handle_start)
    ctl.router.add_delete("/admin/nodes/{fp}/quiet", quiet.handle_end)
    c = TestClient(TestServer(ctl, host="127.0.0.1"))
    await c.start_server()
    out = {}
    n = add_node()
    r = await c.post(f"/admin/nodes/{FP}/quiet", json={"until": time.time() + 600, "reason": "tempo run"})
    out["start"] = (r.status, await r.json())
    r = await c.post(f"/admin/nodes/{FP}/quiet", json={"until": "later", "reason": "x"})
    out["bad"] = r.status
    r = await c.post(f"/admin/nodes/{FP}/quiet", data="[1]", headers={"Content-Type": "application/json"})
    out["not_object"] = r.status
    n.state, n.busy_reason, n.loaded = "busy", "foreign_vram", {BENCH: 6.0}
    r = await c.delete(f"/admin/nodes/{FP}/quiet")
    out["refused"] = (r.status, await r.json(), n.quiet(time.time()))
    r = await c.delete(f"/admin/nodes/{FP}/quiet", json={"accept_unload": "yes"})
    out["accept_text"] = r.status
    r = await c.delete(f"/admin/nodes/{FP}/quiet", json={"accept_unload": True})
    out["accepted"] = (r.status, await r.json(), n.quiet(time.time()))
    r = await c.delete(f"/admin/nodes/{FP}/quiet")
    out["again"] = (r.status, await r.json())
    n.state, n.loaded = "free", {}
    quiet.start(n, time.time() + 600, "tempo run", time.time())
    r = await c.delete(f"/admin/nodes/{FP}/quiet")
    out["free_end"] = (r.status, await r.json())
    r = await c.delete(f"/admin/nodes/{'0' * 64}/quiet")
    out["unknown"] = r.status
    await c.close()
    return out


PLANNED.clear()
o = asyncio.run(http())
check("POST quiet -> 200 with the quiet view", o["start"][0] == 200 and o["start"][1]["quiet"]["reason"] == "tempo run", str(o["start"]))
check("POST with until as text -> 400", o["bad"] == 400, str(o["bad"]))
check("POST with a JSON array -> 400", o["not_object"] == 400, str(o["not_object"]))
st, body, still = o["refused"]
check("DELETE while busy with the measured model -> 409, quiet stays", st == 409 and still, f"{st} {still}")
check("... names what would be unloaded and the next step", body.get("would_unload") == [BENCH] and "accept_unload" in body.get("next", ""),
      str(body))
check("DELETE with accept_unload as text -> 400", o["accept_text"] == 400, str(o["accept_text"]))
st, body, still = o["accepted"]
check("DELETE with accept_unload: true -> 200, quiet ended, facts of what follows", st == 200 and not still
      and body["then"]["will_unload"] == [BENCH] and body["then"]["prewarm"] is False, str(body))
check("DELETE without a quiet -> 200 was_quiet false", o["again"][0] == 200 and o["again"][1]["was_quiet"] is False, str(o["again"]))
check("DELETE on a free node -> 200, prewarm planned", o["free_end"][0] == 200 and o["free_end"][1]["then"]["prewarm"] is True
      and names("prewarm") == ["quiet_end"], f"{o['free_end']} {PLANNED}")
check("DELETE for an unknown fingerprint -> 404", o["unknown"] == 404, str(o["unknown"]))


# 9) In the real app the quiet routes win over the generic node action
async def routes():
    state.REG = registry.NodeRegistry(os.path.join(HERE, "selftest_quiet_nodes.json"))   # never loaded or saved here
    _, ctl = app.build_apps()
    ctl.router.freeze()
    got = {}
    for method, path in [("POST", f"/admin/nodes/{FP}/quiet"), ("DELETE", f"/admin/nodes/{FP}/quiet"),
                         ("POST", f"/admin/nodes/{FP}/approve")]:
        info = await ctl.router.resolve(make_mocked_request(method, path, app=ctl))
        got[(method, path.rsplit("/", 1)[1])] = info.handler
    return got


g = asyncio.run(routes())
check("POST /admin/nodes/{fp}/quiet -> quiet.handle_start", g[("POST", "quiet")] is quiet.handle_start)
check("DELETE /admin/nodes/{fp}/quiet -> quiet.handle_end", g[("DELETE", "quiet")] is quiet.handle_end)
check("POST /admin/nodes/{fp}/approve still -> registry.handle_node_action", g[("POST", "approve")] is registry.handle_node_action)

print()
print(f"{len(FAILS)} failures" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
