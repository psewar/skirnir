#!/usr/bin/env python3
"""Self-test (Router 0.44.0, agent 0.37.0, ADR-0148/0149): Ollama's runner processes as a fact, and an unload that answers
once their memory is released.

Trigger: the model test bench unloads a node (keep_alive 0) and then starts a large job there. /api/ps is empty at once,
but the runner process keeps the model's main memory for seconds; a job started in that gap pushed the node to 93 % RAM.
The bench waited for an empty /api/ps and polled the node's RAM, a guess. Now the agent reports `ollama_runners` and the
router answers `POST /admin/nodes/{fp}/unload` only once a heartbeat after the unload reports no runner process.
Runs without router, network or Ollama (real config from router/config.example.yaml); Ollama calls are stand-ins."""
import asyncio
import math
import os
import sys
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import app, config, metrics, nodes, perf, poll, registry, state, unload   # noqa: E402
from skirnir_router.outcome import bad_gateway, failed   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.spawn = lambda coro: coro.close()


def no_network(*a, **kw):
    raise AssertionError("no network in this self-test")   # exception-ok: test stub, a call is the failure itself


nodes.nreq = no_network
FP = "e" * 64
MODEL_A, MODEL_B = "example-model:8b", "example-model:32b"


def add_node(name="synthetic-node", fp=FP):
    now = time.time()
    n = nodes.Node(name, {"vram_total_gib": 24.0, "foreign_vram_baseline_gib": 0.0})
    n.fp = fp
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.ollama_runners = {"count": 0, "private_mib": 0}
    state.NODES = {n.name: n}
    state.REG = SimpleNamespace(nodes={fp: {"name": n.name, "state": "approved"}}, save=lambda: None, dirty=False)
    return n


GPU = {"gpu_present": True, "gpu_util_pct": 1, "vram_total_mib": 24564, "vram_used_mib": 900, "vram_free_mib": 23664}

# 1) The heartbeat field: stored when within the schema, dropped (unknown, never a guessed 0) when not
n = add_node()
poll.apply_heartbeat(n, {**GPU, "ollama_runners": {"count": 2, "private_mib": 6200}}, time.time())
check("heartbeat with ollama_runners -> stored", n.ollama_runners == {"count": 2, "private_mib": 6200}
      and n.implausible_fields == [], f"{n.ollama_runners} {n.implausible_fields}")
poll.apply_heartbeat(n, {**GPU, "ollama_runners": {"count": 0, "private_mib": 0, "future_field": 1}}, time.time())
check("count 0 is a fact (released), unknown inner fields ignored", n.ollama_runners == {"count": 0, "private_mib": 0},
      str(n.ollama_runners))
poll.apply_heartbeat(n, dict(GPU), time.time())
check("heartbeat without the field -> None (the agent cannot tell), nothing implausible",
      n.ollama_runners is None and n.implausible_fields == [], f"{n.ollama_runners} {n.implausible_fields}")
for bad in ({"count": -1, "private_mib": 0}, {"count": True, "private_mib": 0}, {"count": "1", "private_mib": 0},
            {"count": 257, "private_mib": 0}, {"count": 1.0, "private_mib": 0}, {"count": 1},
            {"count": 1, "private_mib": -5}, {"count": 1, "private_mib": 1 << 23}, {"count": 1, "private_mib": math.inf},
            {"count": 1, "private_mib": 5.5}, [1, 2], "two", 3):
    n.ollama_runners = {"count": 0, "private_mib": 0}   # a good value before: must not survive a bad report
    poll.apply_heartbeat(n, {**GPU, "ollama_runners": bad}, time.time())
    check(f"rejected: ollama_runners={bad!r} -> unknown, named as implausible, GPU values still applied",
          n.ollama_runners is None and "ollama_runners" in n.implausible_fields and n.vram_total_reported_gib is not None,
          f"{n.ollama_runners} {n.implausible_fields}")

# 2) Freshness: a stale heartbeat's count 0 does not read as released
n = add_node()
now = time.time()
n.ollama_runners = {"count": 0, "private_mib": 0}
check("fresh heartbeat -> runners known", n.runners_known(now) == {"count": 0, "private_mib": 0})
n.hb_ts = now - state.CFG.hb_stale_s - 1
check("rejected: stale heartbeat -> runners unknown", n.runners_known(now) is None)
check("node view shows None for the stale fact", n.snapshot(now)["ollama_runners"] is None)
n.hb_ts = now
n.ollama_runners = {"count": 1, "private_mib": 3120}
check("node view shows the fresh fact", n.snapshot(now)["ollama_runners"] == {"count": 1, "private_mib": 3120})

# 3) Metrics: one line per reporting node, none for a node that cannot tell
other = nodes.Node("synthetic-node-2", {"vram_total_gib": 24.0})
other.hb_ts = now
state.NODES = {n.name: n, other.name: other}
text = metrics.render()
check("metric skirnir_node_ollama_runners for the reporting node",
      'skirnir_node_ollama_runners{node="synthetic-node"} 1' in text, "")
check("metric skirnir_node_ollama_runner_private_gib in GiB",
      'skirnir_node_ollama_runner_private_gib{node="synthetic-node"} 3.047' in text, "")
check("rejected: no runner metric for a node without the fact", 'node="synthetic-node-2"} ' not in
      "\n".join(line for line in text.splitlines() if line.startswith("skirnir_node_ollama_runner")), "")

# 4) Request validation: facts, not exceptions
check("timeout_s default", unload.parse_timeout({}) == unload.DEFAULT_TIMEOUT_S)
check("timeout_s 30 accepted", unload.parse_timeout({"timeout_s": 30}) == 30.0)
for bad in (0, 0.5, 601, True, "60", math.inf, math.nan, None, [60]):
    p = unload.parse_timeout({"timeout_s": bad})
    check(f"rejected: timeout_s={bad!r} -> 400", failed(p) and p.status == 400, str(p))

# 5) Preconditions are checked before anything is unloaded
n = add_node()
now = time.time()
check("an online node with the fact may be unloaded", unload.refusal(n, now) is None)
n.state = "offline"
check("rejected: offline node", "offline" in (unload.refusal(n, now) or {}).get("error", ""))
n.state, n.openai_upstream = "free", True
check("rejected: OpenAI-compatible upstream", "OpenAI" in (unload.refusal(n, now) or {}).get("error", ""))
n.openai_upstream, n.ollama_runners = False, None
check("rejected: the agent does not report runner processes",
      "does not report" in (unload.refusal(n, now) or {}).get("error", ""))
n.ollama_runners = {"count": 0, "private_mib": 0}
state.MEASURING[MODEL_A] = {"node": n.name, "step": "loading @8192"}
check("rejected: a measurement runs on the node", MODEL_A in (unload.refusal(n, now) or {}).get("error", ""))
state.MEASURING[MODEL_A]["node"] = "synthetic-node-2"
check("a measurement on another node does not block", unload.refusal(n, now) is None)
state.MEASURING.clear()
state.BENCHING[MODEL_B] = {"node": n.name, "step": "run 1/2"}
check("rejected: a benchmark runs on the node", MODEL_B in (unload.refusal(n, now) or {}).get("error", ""))
state.BENCHING.clear()


# 6) The unload itself, with a stand-in Ollama and a simulated clock that delivers heartbeats
class Bench:
    """Stand-in for Ollama on the node (perf._ps_models, perf._ollama) and for the agent's heartbeats over time."""

    def __init__(self, node, loaded, empty_after=2, beats=(), ps_fails=False, at_start=None):
        self.node, self.ps, self.empty_after, self.beats, self.ps_fails = node, list(loaded), empty_after, list(beats), ps_fails
        self.t, self.calls, self.polls, self.unloads = 1000.0, [], 0, []
        node.loaded = {m: 4.0 for m in loaded}
        # the last heartbeat before the unload, on this clock: by default one runner per loaded model
        node.hb_ts = self.t
        node.ollama_runners = at_start if at_start is not None else {"count": len(loaded), "private_mib": 3000 * len(loaded)}

    def clock(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        while self.beats and self.beats[0][0] <= self.t:   # heartbeats due by now: (time, runners or None, state)
            at, runners, node_state = self.beats.pop(0)
            self.node.hb_ts, self.node.ollama_runners = at, runners
            if node_state:
                self.node.state = node_state

    async def ps_models(self, node):
        self.calls.append("ps")
        if self.ps_fails:
            return bad_gateway("Ollama /api/ps: synthetic failure")
        if self.unloads:
            self.polls += 1
            if self.polls > self.empty_after:
                self.ps = []
                self.node.loaded = {}
        return [{"name": m, "size_vram": 4 << 30} for m in self.ps]

    async def ollama(self, node, method, path, body=None, timeout=60, answer=True):
        self.calls.append((method, path, dict(body or {})))
        self.unloads.append(body["model"])
        return {}


def run(bench, timeout_s=30):
    perf_ps, perf_ollama = perf._ps_models, perf._ollama
    perf._ps_models, perf._ollama = bench.ps_models, bench.ollama
    try:
        return asyncio.run(unload.unload_and_wait(bench.node, timeout_s, clock=bench.clock, sleep=bench.sleep))
    finally:
        perf._ps_models, perf._ollama = perf_ps, perf_ollama


def runners(count, mib=0):
    return {"count": count, "private_mib": mib}


# a) the normal case: /api/ps empties first, the runner exits seconds later
n = add_node()
b = Bench(n, [MODEL_B, MODEL_A], empty_after=2, at_start=runners(2, 7000),
          beats=[(1001.0, runners(1, 3500), None), (1002.0, runners(1, 3100), None), (1004.0, runners(0), None)])
r = run(b)
BOTH = sorted([MODEL_A, MODEL_B])
check("released: 200 body with released true, reason released, both models unloaded",
      r["released"] is True and r["ok"] is True and r["reason"] == "released" and r["unloaded"] == BOTH, str(r))
check("each loaded model got keep_alive 0", [c for c in b.calls if c != "ps"] ==
      [("post", "/api/generate", {"model": m, "keep_alive": 0}) for m in BOTH], str(b.calls))
check("... and the answer came only with the heartbeat that reported no runner (t=1004), not when /api/ps emptied",
      b.t >= 1004.0 and r["waited_s"] >= 4.0 and r["ollama_runners"] == runners(0), f"t={b.t} {r}")

# b) rejected: a count 0 from a heartbeat older than the empty /api/ps is not the fact that the memory is released
n = add_node()
b = Bench(n, [MODEL_A], empty_after=1, at_start=runners(0),   # e.g. reported while the runner had not started yet
          beats=[(1002.0, runners(1, 3000), None), (1003.5, runners(0), None)])
r = run(b)
check("rejected: an older heartbeat's 0 does not count; released with the later one", r["released"] is True and b.t >= 1003.5,
      f"t={b.t} {r}")

# c) rejected: the runner never ends -> 503 facts with reason timeout
n = add_node()
b = Bench(n, [MODEL_A], empty_after=1, beats=[(1000.0 + i, runners(1, 3000), None) for i in range(1, 40)])
r = run(b, timeout_s=10)
check("rejected: runner still alive at the deadline -> released false, reason timeout, next step named",
      r["released"] is False and r["ok"] is False and r["reason"] == "timeout" and r["ollama_runners"] == runners(1, 3000)
      and r["next"] and r["error"], str(r))
check("... after timeout_s, not earlier", 1010.0 <= b.t <= 1011.0, str(b.t))

# d) rejected: /api/ps never empties (a request keeps the model) -> timeout with what is still loaded
n = add_node()
b = Bench(n, [MODEL_A], empty_after=10 ** 6, beats=[(1000.0 + i, runners(1, 3000), None) for i in range(1, 40)])
r = run(b, timeout_s=5)
check("rejected: /api/ps still lists the model at the deadline -> timeout, loaded named", r["reason"] == "timeout"
      and r["loaded"] == [MODEL_A] and r["released"] is False, str(r))

# e) rejected: the agent stops reporting runners (Ollama restarting) -> not_reported, never "released"
n = add_node()
b = Bench(n, [MODEL_A], empty_after=1, beats=[(1001.0, runners(1, 3000), None), (1002.0, None, None)])
r = run(b)
check("rejected: fact disappears -> released false, reason not_reported", r["reason"] == "not_reported"
      and r["released"] is False and r["ollama_runners"] is None, str(r))

# f) rejected: the node goes offline while waiting
n = add_node()
b = Bench(n, [MODEL_A], empty_after=1, beats=[(1001.0, runners(1, 3000), "offline")])
r = run(b)
check("rejected: node offline while waiting -> reason offline", r["reason"] == "offline" and r["released"] is False, str(r))

# g) Ollama cannot be asked -> a Problem (502), nothing claimed
n = add_node()
b = Bench(n, [MODEL_A], ps_fails=True)
r = run(b)
check("rejected: /api/ps fails -> Problem 502, no unload sent", failed(r) and r.status == 502
      and not [c for c in b.calls if c != "ps"], str(r))

# h) nothing loaded: the answer still waits for a heartbeat without runners
n = add_node()
b = Bench(n, [], at_start=runners(1, 800),   # a runner that is still exiting or starting
          beats=[(1001.5, runners(0), None)])
r = run(b)
check("nothing loaded -> no unload sent, released with the next heartbeat without runners",
      r["released"] is True and r["unloaded"] == [] and b.unloads == [] and b.t >= 1001.5, f"t={b.t} {r}")


# 7) Over HTTP: rejected cases answer before anything is sent to Ollama; the success answers 200, a timeout 503
async def http():
    ctl = web.Application()
    ctl.router.add_post("/admin/nodes/{fp}/unload", unload.handle_unload)
    c = TestClient(TestServer(ctl, host="127.0.0.1"))
    await c.start_server()
    out = {}
    n = add_node()
    calls = []

    async def ps(node):
        calls.append("ps")
        return []

    async def ollama(*a, **kw):
        calls.append("generate")
        return {}

    perf_ps, perf_ollama, step = perf._ps_models, perf._ollama, unload.WAIT_STEP_S
    perf._ps_models, perf._ollama, unload.WAIT_STEP_S = ps, ollama, 0.05
    try:
        out["unknown"] = (await c.post(f"/admin/nodes/{'0' * 64}/unload")).status
        state.REG.nodes[FP]["state"] = "pending"
        out["not_approved"] = (await c.post(f"/admin/nodes/{FP}/unload")).status
        state.REG.nodes[FP]["state"] = "approved"
        out["bad_timeout"] = (await c.post(f"/admin/nodes/{FP}/unload", json={"timeout_s": 0})).status
        out["not_object"] = (await c.post(f"/admin/nodes/{FP}/unload", data="[1]",
                                          headers={"Content-Type": "application/json"})).status
        out["malformed"] = (await c.post(f"/admin/nodes/{FP}/unload", data="{\"timeout_s\": 5",
                                         headers={"Content-Type": "application/json"})).status
        n.ollama_runners = None
        r = await c.post(f"/admin/nodes/{FP}/unload")
        out["no_fact"] = (r.status, await r.json())
        out["calls_after_refusals"] = list(calls)
        n.ollama_runners = runners(1, 3000)

        async def agent_reports_exit():
            await asyncio.sleep(0.3)
            n.hb_ts, n.ollama_runners = time.time(), runners(0)

        exit_report = asyncio.create_task(agent_reports_exit())
        r = await c.post(f"/admin/nodes/{FP}/unload", json={"timeout_s": 5})
        out["released"] = (r.status, await r.json())
        await exit_report
        out["journal"] = [e for e in list(state.DECISIONS) if e.get("event") == "unload"][-1:]
        n.ollama_runners = runners(1, 3000)
        n.hb_ts = time.time()

        async def keep_beating():
            for _ in range(30):
                await asyncio.sleep(0.05)
                n.hb_ts = time.time()

        beats = asyncio.create_task(keep_beating())
        first = asyncio.create_task(c.post(f"/admin/nodes/{FP}/unload", json={"timeout_s": 1}))
        await asyncio.sleep(0.2)
        calls_before_second = len(calls)
        r = await c.post(f"/admin/nodes/{FP}/unload", json={"timeout_s": 1})
        out["second"] = (r.status, await r.json(), len(calls) - calls_before_second <= 1)
        r = await first
        out["timeout"] = (r.status, await r.json())
        out["running_after"] = set(unload.RUNNING)
        await beats

        # the caller hangs up: aiohttp cancels the handler; the node must not stay marked as unloading
        async def hang_up():
            calls.clear()
            pending = asyncio.create_task(unload.handle_unload(make_mocked_request(
                "POST", f"/admin/nodes/{FP}/unload", match_info={"fp": FP})))
            await asyncio.sleep(0.2)
            marked = n.name in unload.RUNNING
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            return marked, n.name in unload.RUNNING

        out["cancel"] = await hang_up()
    finally:
        perf._ps_models, perf._ollama, unload.WAIT_STEP_S = perf_ps, perf_ollama, step
        await c.close()
    return out


o = asyncio.run(http())
check("unknown fingerprint -> 404", o["unknown"] == 404, str(o["unknown"]))
check("node not approved -> 409", o["not_approved"] == 409, str(o["not_approved"]))
check("bad timeout_s -> 400", o["bad_timeout"] == 400, str(o["bad_timeout"]))
check("body not an object -> 400", o["not_object"] == 400, str(o["not_object"]))
check("rejected: malformed JSON body -> 400, not taken as an empty body", o["malformed"] == 400, str(o["malformed"]))
check("rejected: no runner fact -> 409 with the facts and the next step",
      o["no_fact"][0] == 409 and o["no_fact"][1]["ollama_runners"] is None and o["no_fact"][1]["next"], str(o["no_fact"]))
check("... and none of the refusals asked Ollama anything", o["calls_after_refusals"] == [], str(o["calls_after_refusals"]))
check("released -> 200 with released true", o["released"][0] == 200 and o["released"][1]["released"] is True, str(o["released"]))
check("... journal entry event unload", bool(o["journal"]) and o["journal"][0]["released"] is True, str(o["journal"]))
check("rejected: runner stays -> 503 with released false and reason timeout",
      o["timeout"][0] == 503 and o["timeout"][1]["released"] is False and o["timeout"][1]["reason"] == "timeout", str(o["timeout"]))
check("rejected: a second unload of the same node while one runs -> 409, nothing sent for it",
      o["second"][0] == 409 and "already running" in o["second"][1]["error"] and o["second"][2], str(o["second"]))
check("... and the node is free for the next unload once the first answered", o["running_after"] == set(), str(o["running_after"]))
check("a cancelled unload (caller hung up) does not leave the node marked", o["cancel"] == (True, False), str(o["cancel"]))


# 8) In the real app the unload route wins over the generic node action, behind login and the CSRF header
async def routes():
    state.REG = registry.NodeRegistry(os.path.join(HERE, "selftest_ollama_runners_nodes.json"))   # never loaded or saved here
    _, ctl = app.build_apps()
    ctl.router.freeze()
    got = {}
    for path in (f"/admin/nodes/{FP}/unload", f"/admin/nodes/{FP}/approve"):
        info = await ctl.router.resolve(make_mocked_request("POST", path, app=ctl))
        got[path.rsplit("/", 1)[1]] = info.handler
    return got


g = asyncio.run(routes())
check("POST /admin/nodes/{fp}/unload -> unload.handle_unload", g["unload"] is unload.handle_unload)
check("POST /admin/nodes/{fp}/approve still -> registry.handle_node_action", g["approve"] is registry.handle_node_action)


async def guarded():
    from skirnir_router import auth
    users = state.CFG.control_users
    state.CFG.control_users = {"tester": auth.hash_password("synthetic-pass", iterations=1000)}
    add_node()
    calls = []

    async def ps(node):
        calls.append("ps")
        return []

    perf_ps, perf._ps_models = perf._ps_models, ps
    _, ctl = app.build_apps()
    c = TestClient(TestServer(ctl, host="127.0.0.1"))
    await c.start_server()
    basic = {"Authorization": "Basic " + __import__("base64").b64encode(b"tester:synthetic-pass").decode()}
    got = {}
    try:
        got["anon"] = (await c.post(f"/admin/nodes/{FP}/unload")).status
        got["no_csrf"] = (await c.post(f"/admin/nodes/{FP}/unload", headers=basic)).status
        got["calls"] = list(calls)
    finally:
        perf._ps_models = perf_ps
        await c.close()
        state.CFG.control_users = users
    return got


g = asyncio.run(guarded())
check("rejected: POST unload without credentials -> 401", g["anon"] == 401, str(g))
check("rejected: POST unload without X-Skirnir-UI -> 403", g["no_csrf"] == 403, str(g))
check("... and neither rejected request reached Ollama", g["calls"] == [], str(g))

print()
print(f"{len(FAILS)} failures" if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
