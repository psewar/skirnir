#!/usr/bin/env python3
"""Self-test (Router 0.4.1 / Agent 0.14.x): Ollama state from the agent (OLLAMA framework, poll.apply_ollama_push).

Reproduces the incident from 2026-09-30 after updating to Agent 0.14.0: Ollama was not yet ready when the agent started
(report up=false, node offline), then responded without a loaded model, and the agent sent `ps: null`. The
Router discarded this and kept the last valid report "offline" until it expired. Runs without Router, network, and Ollama."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, poll, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.prewarm_on_online = False   # do not trigger prewarming (no event loop, no node behind it)
poll.evaluate = lambda node, now: None   # the state machine is not the subject here
state.CAPS["qwen3.8:27b"] = ["completion", "tools"]   # Capabilities known: no reloading (would require event loop and node)
QWEN_TAG = {"name": "qwen3.8:27b", "digest": "aa", "size": 17 * 2**30}
QWEN_PS = {"name": "qwen3.8:27b", "digest": "aa", "size_vram": 20 * 2**30, "context_length": 131072, "expires_at": "2026-09-30T22:00:00Z"}

n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
now = time.time()
check("before the first report: Router polls", not n.ollama_push_active(now))

poll.apply_ollama_push(n, {"up": False, "error": "connection refused", "rev": 1}, now)
check("up=false (Ollama still starting): node offline, report counts, Router does not poll", n.state == "offline" and n.ollama_push_active(now)
      and "connection refused" in (n.ollama_push_error or ""))

poll.apply_ollama_push(n, {"up": True, "rev": 2, "tags": [QWEN_TAG], "ps": None}, now + 1)
check("Agent 0.14.0: ps=null treated as empty list -> node free again, models known, nothing loaded",
      n.state == "free" and n.models == {"qwen3.8:27b"} and n.loaded == {} and n.ollama_push_active(now + 1), f"{n.state} {n.models} {n.loaded}")

poll.apply_ollama_push(n, {"up": True, "rev": 3, "tags": [QWEN_TAG], "ps": [QWEN_PS]}, now + 2)
check("Model loaded: size and context length adopted (context sharing from 0.3.7 requires loaded_ctx)",
      abs(n.loaded.get("qwen3.8:27b", 0) - 20) < 0.01 and n.loaded_ctx.get("aa") == 131072, f"{n.loaded} {n.loaded_ctx}")

poll.apply_ollama_push(n, {"up": True, "rev": 4, "tags": "kaputt", "ps": []}, now + 3)
check("unusable report: discarded, state remains, Router polls again immediately (do not keep the old report)",
      not n.ollama_push_active(now + 3) and n.state == "free" and "qwen3.8:27b" in n.loaded, f"push_ts={n.ollama_push_ts}")

poll.apply_ollama_push(n, {"rev": 5}, now + 4)
check("report without up: discarded, Router polls", not n.ollama_push_active(now + 4))

poll.apply_ollama_push(n, {"up": True, "rev": 6, "tags": [QWEN_TAG, {"kein": "name"}, 7], "ps": []}, now + 5)
check("entries without names are dropped, report is valid", n.models == {"qwen3.8:27b"} and n.loaded == {} and n.ollama_push_active(now + 5))
check("fresh up to 45 s, then poll again", n.ollama_push_active(now + 5 + 44) and not n.ollama_push_active(now + 5 + 46))

# --- Swap only after release (Router 0.5.0 / Agent 0.15.0): drain, wait for running requests, then release
import asyncio  # noqa: E402

from skirnir_router import ollamaupdate, registry  # noqa: E402

SENT = []


async def send_ctl(tunnel, msg):
    SENT.append(msg)


class Reg:
    def __init__(self, e):
        self.nodes = {"fp1": e}

    def save(self):
        pass


registry.send_ctl = send_ctl
ollamaupdate.SWAP_WAIT_S = 1.5


async def swap_scenario(inflight_ends):
    SENT.clear()
    k = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
    k.fp, k.tunnel, k.inflight, k.state = "fp1", object(), 1, "free"
    e = {"name": "gpu-desktop", "facts": {"ollama_swap_gate": True}, "ollama_update": {"version": "0.35.0", "state": "ready", "t": time.time()}}
    state.REG = Reg(e)
    t = asyncio.create_task(ollamaupdate.release_swap(k, "0.35.0"))
    await asyncio.sleep(0.6)
    before = (list(SENT), time.time() < k.draining_until)
    if inflight_ends:
        k.inflight = 0
    await t
    return before, list(SENT), e["ollama_update"], k


(before, after, u, k) = asyncio.run(swap_scenario(True))
check("active request: node excluded from routing, but no release yet", before == ([], True), str(before))
check("request finished: swap released", after == [{"t": "ollama-swap", "version": "0.35.0"}] and k.swap_pending is None, str(after))
(before, after, u, k) = asyncio.run(swap_scenario(False))
check("node does not become empty: no release, task failed, node back in routing",
      after == [] and u["state"] == "failed" and "nicht leer" in u["message"] and time.time() >= k.draining_until, f"{after} {u}")
state.REG = Reg({"name": "gpu-desktop", "facts": {}, "ollama_update": {"version": "0.35.0", "state": "downloading", "t": time.time()}})
k = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
k.fp = "fp1"
check("no prewarming during an Ollama update (update_running)", ollamaupdate.update_running(k))
state.REG.nodes["fp1"]["ollama_update"]["state"] = "done"
check("prewarming allowed again after the update", not ollamaupdate.update_running(k))
check("Agent without release step (< 0.15.0) detected", not ollamaupdate.swap_gate({"facts": {}}) and ollamaupdate.swap_gate({"facts": {"ollama_swap_gate": True}}))

# Update complete (agent reports target version): prewarm once, as prewarming during restart was skipped during the update
WARMED = []


async def prewarm_stub(node, delay, reason, since=None):
    WARMED.append(reason)


async def report_done(state_now):
    WARMED.clear()
    k = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
    k.fp, k.state = "fp1", state_now
    state.REG = Reg({"name": "gpu-desktop", "facts": {"ollama_swap_gate": True},
                     "ollama_update": {"version": "0.35.0", "state": "applied", "t": time.time()}})
    ollamaupdate.note_version(k, "0.35.0", time.time(), source="agent")
    await asyncio.sleep(0.05)
    return state.REG.nodes["fp1"]["ollama_update"]["state"], list(WARMED)


poll.prewarm = prewarm_stub
state.CFG.prewarm_on_online = True   # disabled above for push checks
r = asyncio.run(report_done("free"))
check("update done, node free: prewarm once", r == ("done", ["after Ollama update"]), str(r))
check("update done, node busy (game): do not prewarm", asyncio.run(report_done("busy")) == ("done", []))

print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
