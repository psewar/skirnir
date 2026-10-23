#!/usr/bin/env python3
"""Self-test for base codes for "no node" (scheduler.blockers / scheduler.unavailable). Runs without router and
without network, using stubs for nodes and configuration.

Trigger 2026-09-29: a game occupied 5 GiB VRAM on gpu-desktop, qwen3.6@131k no longer fit the budget, but the node
was marked "free". The router only replied "no node available"; the agent client could neither wait nor state the reason. Key distinction here: gpu_busy only if foreign VRAM (or busy state) is truly the cause - a model that doesn't fit even on an empty card is model_too_large, and waiting for it would be pointless.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import scheduler as S   # noqa: E402
from skirnir_router import state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class Cfg:
    reserve = {"free": 1.0, "busy": 2.0}
    need = {"qwen3.6:35b-a3b": 24.0, "riesig:1t": 40.0, "klein:8b": 6.0}

    def need_gib(self, model, ctx, node=None):
        return self.need[model]


class FakeNode:
    """Only what _node_block reads. budget_gib like Node.budget_gib: free VRAM + Ollama's own - reserve."""

    def __init__(self, name, models, st="free", free=30.0, total=31.8, foreign=0.0, loaded=(), breaker="closed",
                 draining_until=0.0, busy_reason="", gpu_util=0, game=None, memory=None, mem_pressure=False):
        self.name, self.models, self.state = name, set(models), st
        self.vram_free_gib, self.vram_total_gib, self._foreign = free, total, foreign
        self.loaded = {m: 20.0 for m in loaded}
        self.breaker, self.draining_until, self.busy_reason, self.gpu_util = breaker, draining_until, busy_reason, gpu_util
        self.game, self.memory, self.mem_pressure = game, memory, mem_pressure

    def foreign_vram_gib(self):
        return self._foreign

    def breaker_would_allow(self, now):
        return self.breaker == "closed"

    def offers(self, model):   # router 0.15.0: no standby switch on this fake node
        return model in self.models

    def standby_switch(self, model):
        return False

    def releases_server_for(self, model):   # router 0.16.2: no OpenAI-compatible server on this fake node
        return False

    def is_loaded(self, model):
        return model in self.loaded

    def loaded_context(self, model):
        return None

    def budget_gib(self, now, model):
        return self.vram_free_gib + sum(self.loaded.values()) - state.CFG.reserve.get(self.state, 1.0)


def add_node(*ns):
    state.NODES.clear()
    for n in ns:
        state.NODES[n.name] = n


state.CFG = Cfg()
NIGHT = [{"model": "qwen3.6:35b-a3b", "num_ctx": 131072, "busy_ok": False}]

# --- case from 2026-09-29: free, but a game occupies 5 GiB -> budget 19.4 < 24 ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=20.4, foreign=5.0, gpu_util=35))
u = S.unavailable(NIGHT, None, 0)
b = u["blockers"][0] if u["blockers"] else {}
check("free + foreign VRAM takes the space -> gpu_busy", u["code"] == S.GPU_BUSY, str(u))
check("... with numbers for the client", b.get("need_gib") == 24.0 and b.get("budget_gib") == 19.4 and b.get("foreign_vram_gib") == 5.0, str(b))
check("... reason foreign_vram, Retry-After set", b.get("busy_reason") == "foreign_vram" and u["retry_after_s"] == S.RETRY_AFTER_S[S.GPU_BUSY], str(u))
check("... plaintext names node, foreign GiB and requirement", all(s in u["detail"] for s in ("gpu-desktop", "5.0 GiB", "24.0 GiB")), u["detail"])

# --- counter-check: without foreign VRAM, space is missing for another reason -> not gpu_busy ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=20.4, foreign=0.0))
check("space missing without foreign VRAM -> vram_full, not gpu_busy", S.unavailable(NIGHT, None, 0)["code"] == S.VRAM_FULL)

# --- foreign VRAM present, but would not have fit without it -> vram_full ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=10.0, foreign=2.0))
check("foreign VRAM too small to explain the gap -> vram_full", S.unavailable(NIGHT, None, 0)["code"] == S.VRAM_FULL)

# --- model doesn't fit on empty card -> model_too_large, no Retry-After ---
huge = [{"model": "riesig:1t", "num_ctx": 8192, "busy_ok": False}]
add_node(FakeNode("gpu-desktop", ["riesig:1t"], free=20.0, foreign=5.0))
u = S.unavailable(huge, None, 0)
check("never fits -> model_too_large without Retry-After", u["code"] == S.MODEL_TOO_LARGE and u["retry_after_s"] is None, str(u))

# --- node busy (game with GPU load) -> gpu_busy with busy_reason ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], st="busy", free=25.0, foreign=4.3, busy_reason="gpu_util", gpu_util=65))
u = S.unavailable(NIGHT, None, 0)
check("busy durch gpu_util -> gpu_busy", u["code"] == S.GPU_BUSY and u["blockers"][0]["busy_reason"] == "gpu_util", str(u))

# --- Agent >= 0.17.0: game as fact -> gpu_busy names the game ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], st="busy", busy_reason="game", game={"name": "Diablo IV.exe"}))
u = S.unavailable(NIGHT, None, 0)
check("busy due to game -> gpu_busy with game name", u["code"] == S.GPU_BUSY and u["blockers"][0].get("game") == "Diablo IV.exe"
      and "Diablo IV.exe" in u["detail"], str(u))

# --- memory tight: cold load blocked, warm load remains serviceable ---
low_mem = {"ram_available_gib": 2.5, "commit_free_gib": 1.8, "ram_total_gib": 64.0}
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=30.0, memory=low_mem, mem_pressure=True))
u = S.unavailable(NIGHT, None, 0)
check("memory tight + cold -> memory_pressure with numbers", u["code"] == S.MEMORY_PRESSURE
      and u["blockers"][0].get("commit_free_gib") == 1.8 and "ram_total_gib" not in u["blockers"][0], str(u))
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=30.0, memory=low_mem, mem_pressure=True, loaded=["qwen3.6:35b-a3b"]))
check("memory tight + warm loaded -> no obstacle", S.blockers(NIGHT, None, 0) == [])

# --- busy_ok tier runs despite busy: no blocker ---
add_node(FakeNode("gpu-desktop", ["klein:8b"], st="busy", free=25.0))
check("busy_ok tier with space -> no obstacles", S.blockers([{"model": "klein:8b", "num_ctx": 8192, "busy_ok": True}], None, 0) == [])

# --- warm loaded: no budget needed, no blocker ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=1.0, foreign=5.0, loaded=["qwen3.6:35b-a3b"]))
check("warm loaded model is no obstacle, even with full VRAM", S.blockers(NIGHT, None, 0) == [])

# --- two nodes: one offline, one busy -> gpu_busy wins (resolves itself) ---
add_node(FakeNode("mary", ["qwen3.6:35b-a3b"], st="offline"),
       FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], st="busy", busy_reason="foreign_vram"))
u = S.unavailable(NIGHT, None, 0)
check("offline + busy -> gpu_busy, both listed as obstacle", u["code"] == S.GPU_BUSY and len(u["blockers"]) == 2, str(u))

# --- only offline ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], st="offline"))
check("only offline -> node_offline", S.unavailable(NIGHT, None, 0)["code"] == S.NODE_OFFLINE)

# --- Agent-Update / Breaker ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], draining_until=10.0))
check("agent update running -> node_draining", S.unavailable(NIGHT, None, 0)["code"] == S.NODE_DRAINING)
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], breaker="open"))
check("Breaker offen -> node_failing", S.unavailable(NIGHT, None, 0)["code"] == S.NODE_FAILING)

# --- no node has the model ---
add_node(FakeNode("gpu-desktop", ["klein:8b"]))
check("Model nowhere present -> model_missing", S.unavailable(NIGHT, None, 0)["code"] == S.MODEL_MISSING)

# --- Kandidat war da, Versuch scheiterte (tried) -> attempt_failed ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=30.0))
u = S.unavailable(NIGHT, None, 0, exclude={"gpu-desktop"})
check("operable node in tried -> attempt_failed", u["code"] == S.ATTEMPT_FAILED, str(u))

# --- client context smaller than the tier is adopted ---
add_node(FakeNode("gpu-desktop", ["qwen3.6:35b-a3b"], free=20.4, foreign=5.0))
check("client's num_ctx is in the obstacle", S.blockers(NIGHT, 32768, 0)[0]["num_ctx"] == 32768)

print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
