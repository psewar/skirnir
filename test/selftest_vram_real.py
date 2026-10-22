#!/usr/bin/env python3
"""Self-test of real VRAM accounting (Router 0.3.5). Runs without Router and network, using the real Node and
Config classes, but without Ollama.

Measurements from 2026-09-30 on gpu-desktop (RTX 5090, 31.8 GiB; fully unloaded per run, delta per nvidia-smi, Desktop
idle 0.6 GiB): qwen3.6:35b-a3b uses real 24.93 / 26.43 / 27.93 GiB at 131k / 196k / 262k, /api/ps reports only
20.79 / 20.98 / 21.16. laguna-xs-2.1 real 29.71 GiB at 131k (ps 19.23), partially in RAM at 196k.

The counter-tests show what went wrong with the old calculation (catalog and accounting from /api/ps): the switch
131k -> 196k of the same model fit in reality, but the Router rejected it; and the unreported remainder appeared as foreign VRAM.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import admin, config, nodes, perf, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


QWEN = "qwen3.6:35b-a3b"
MEASURED = {131072: (24.93, 20.79, 20.79), 196608: (26.43, 20.98, 20.98), 262144: (27.93, 21.16, 21.16)}


class Cfg(config.Config):
    """Only what need_gib/real_gib/Node need - no file."""

    def __init__(self, models, roles=None):
        self.models = models
        self.roles = roles or {}
        self.overhead_gib = 0.8
        self.reserve = {"free": 1.0, "busy": 2.0}
        self.hb_stale_s = 10
        self.vram_settle_s = 8


def add_node(used, loaded_ctx=131072, ps=20.79, baseline=1.0):
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "foreign_vram_baseline_gib": baseline})
    n.state = "free"
    n.hb_ts = time.time()
    n.vram_used_gib, n.vram_free_gib = used, 31.8 - used
    n.digest_of = {QWEN: "sha-qwen"}
    n.loaded = {QWEN: ps}
    n.loaded_digest = {"sha-qwen": ps}
    n.loaded_ctx = {"sha-qwen": loaded_ctx}
    return n


# --- Regression line from the real points ---
w, kv = perf.fit_linear([(c, v[0]) for c, v in MEASURED.items()])
check("Gerade: kv 0,0229 GiB/1k", abs(kv - 0.0229) < 0.0005, f"{kv:.4f}")
check("Gerade: Grundwert 21,93 GiB", abs(w - 21.93) < 0.05, f"{w:.2f}")
e = perf.catalog_entry(MEASURED, "gpu-desktop", real=True)
check("Catalog entry marked real + keeps both measurement series", e["real"] is True and e["vram_real_gib"]["196608"] == 26.43
      and e["vram_ps_gib"]["196608"] == 20.98 and e["source"] == "measured (nvidia-smi)", str(e))

# --- Partial offloading does not count for the line ---
lag = perf.catalog_entry({8192: (27.0, 19.0, 19.0), 32768: (27.6, 19.1, 19.1), 131072: (29.71, 19.23, 19.23),
                          196608: (29.65, 14.17, 19.77)}, "gpu-desktop", real=True)
check("laguna: 196k reported as partial_offload", lag.get("partial_offload") == [196608], str(lag.get("partial_offload")))
check("laguna: offloaded point counts with the RAM share (kv > 0)", lag["kv_gib_per_1k"] > 0.01, str(lag))
# 0.3.5 excluded the offloaded point: granite4.2:30b (8k fits, 32k partially in RAM) stood at flat 21 GiB
gra = perf.catalog_entry({8192: (21.21, 20.72, 20.72), 32768: (29.46, 26.1, 28.98)}, "gpu-desktop", real=True)
need32 = gra["weights_gib"] + gra["kv_gib_per_1k"] * 32.768
check("granite case: demand at 32k >= GPU part + RAM part (29.46 + 2.88)", need32 >= 32.3, f"{need32:.2f} {gra}")

# --- Measurement points: fixed + every context of the roles ---
state.CFG = Cfg({}, {"nacht:latest": {"tiers": [{"model": QWEN, "num_ctx": 196608}]},
                     "assist:latest": {"tiers": [{"model": QWEN, "num_ctx": 196608}, {"model": QWEN, "num_ctx": 32768}]},
                     "cloud:latest": {"tiers": [{"model": "openai:gpt-5-mini", "num_ctx": 128000, "cloud": "openai"}]}})
check("Measurement points: 8k, 32k and 196k of the roles", perf.measure_ctxs(QWEN) == [8192, 32768, 196608], str(perf.measure_ctxs(QWEN)))

# --- Anrechnung geladener Modelle ---
REAL = {QWEN: {"weights_gib": round(w, 2), "kv_gib_per_1k": round(kv, 4), "real": True}}
OLD = {QWEN: {"weights_gib": 20.6, "kv_gib_per_1k": 0.001}}   # the catalog up to 0.3.4 (from /api/ps)
state.NODES.clear()

state.CFG = Cfg(REAL)
n = add_node(used=25.52)
check("real_gib at 131k = 24.93", abs(state.CFG.real_gib(QWEN, 131072) - 24.93) < 0.05, f"{state.CFG.real_gib(QWEN, 131072):.2f}")
check("loaded model counted with actual occupancy", abs(n.ollama_vram_gib() - 24.93) < 0.05, f"{n.ollama_vram_gib():.2f}")
need196 = state.CFG.need_gib(QWEN, 196608, n)
check("measured: demand without fit surcharge (26.43 at 196k)", abs(need196 - 26.43) < 0.05, f"{need196:.2f}")
check("unmeasured: fit surcharge remains", abs(Cfg(OLD).need_gib(QWEN, 196608) - (20.6 + 0.001 * 196.608 + 0.8)) < 0.01)
budget = n.budget_gib(time.time(), QWEN)
check("Wechsel 131k -> 196k passt (Bedarf <= Budget)", need196 <= budget, f"need {need196:.2f} budget {budget:.2f}")
check("no phantom foreign VRAM with loaded model", n.foreign_vram_gib() < 0.1, f"{n.foreign_vram_gib():.2f}")

state.CFG = Cfg(OLD)
n_old = add_node(used=25.52)
need_old = Cfg(REAL).need_gib(QWEN, 196608, n_old)   # the real demand, against the old accounting
budget_old = n_old.budget_gib(time.time(), QWEN)
check("Counter-test: old accounting rejects the real switch", need_old > budget_old, f"need {need_old:.2f} budget {budget_old:.2f}")
check("Control check: old remainder appears as foreign VRAM", n_old.foreign_vram_gib() > 3.0, f"{n_old.foreign_vram_gib():.2f}")
check("without real measurement it stays at /api/ps (backward compatible)", abs(n_old.ollama_vram_gib() - 20.79) < 0.01)

# --- Game without loaded model: fully visible with realistic baseline ---
state.CFG = Cfg(REAL)
empty = add_node(used=11.04, baseline=1.0)
empty.loaded, empty.loaded_digest, empty.loaded_ctx = {}, {}, {}
check("Game without model: 10 GiB foreign (baseline 1.0)", abs(empty.foreign_vram_gib() - 10.04) < 0.05, f"{empty.foreign_vram_gib():.2f}")
empty6 = add_node(used=11.04, baseline=6.0)
empty6.loaded, empty6.loaded_digest, empty6.loaded_ctx = {}, {}, {}
check("Counter-test: with old baseline 6.0 only 5 GiB (as on 2026-09-29)", abs(empty6.foreign_vram_gib() - 5.04) < 0.05,
      f"{empty6.foreign_vram_gib():.2f}")

# --- UI stores the catalog: measured entries remain ---
cur = {"models": {QWEN: {"weights_gib": 21.93, "kv_gib_per_1k": 0.0229, "real": True, "vram_real_gib": {"196608": 26.43}}}}
same = admin._merge_overrides(cur, {"models": {QWEN: {"weights_gib": 21.93, "kv_gib_per_1k": 0.0229}}})
check("UI save with same numbers keeps the measurement", same["models"][QWEN].get("real") is True
      and same["models"][QWEN].get("vram_real_gib") == {"196608": 26.43})
other = admin._merge_overrides(cur, {"models": {QWEN: {"weights_gib": 22.5, "kv_gib_per_1k": 0.0229}}})
check("UI save with changed numbers replaces (operator wins)", other["models"][QWEN] == {"weights_gib": 22.5, "kv_gib_per_1k": 0.0229})

# --- Baseline only when memory is truly free (reproducing error from 0.3.5) ---
class Clock:
    """Virtual time for perf: it runs only when perf sleeps. Until 2026-09-30 the replica ran on the wall clock
    (Heartbeat 0.2 s, query 0.5 s, card 1.0 s full): whether the query at ~1.0 s saw the full or already empty
    Heartbeat was decided by Windows timer resolution - the counter-test read 0.9 in 2 of 3 runs."""

    def __init__(self, t0=1000.0):
        self.t = t0

    def time(self):
        return self.t

    async def sleep(self, s):
        self.t += s
        await asyncio.sleep(0)


class SlowFree:
    """Heartbeat every 2 s (Agent pace), derived from the virtual clock. Unloaded at t0 (/api/ps is already empty),
    the card stays full for 5 s (26.9 GiB) and drops to 0.9 GiB only then. After unloading, two identical
    Heartbeats (t0+2, t0+4) occur with the card still full - exactly what 0.3.5 considered "quiet"."""

    TICK, FULL_FOR = 2.0, 5.0

    def __init__(self, clock):
        self.clock, self.t0 = clock, clock.time()

    @property
    def hb_ts(self):
        return self.t0 + (self.clock.time() - self.t0) // self.TICK * self.TICK

    @property
    def vram_used_gib(self):
        return 26.9 if self.hb_ts - self.t0 < self.FULL_FOR else 0.9


def with_clock(do_read):
    """`read(card, clock)` against a freshly unloaded card; perf.time and perf.asyncio.sleep run on the clock."""
    clock = Clock()

    class Asyncio:
        sleep = staticmethod(clock.sleep)

        def __getattr__(self, name):
            return getattr(asyncio, name)

    perf.time, perf.asyncio = clock, Asyncio()
    try:
        return asyncio.run(do_read(SlowFree(clock), clock))
    finally:
        perf.time, perf.asyncio = time, asyncio


state.CFG.vram_settle_s = 0   # waiting for it to free up alone must suffice, not the tail
empty_read = with_clock(lambda k, clock: perf._empty_used(k, before=26.9, unloaded=21.0, timeout=60))
check("Baseline is read only after freeing (0.9 instead of 26.9 GiB)", abs(empty_read - 0.9) < 0.01, f"{empty_read}")
old_read = with_clock(lambda k, clock: perf._stable_used(k, clock.time(), timeout=60))   # 0.3.5: zwei gleiche Heartbeats
check("Counter-test: the path from 0.3.5 reads the still full card (26.9)", abs(old_read - 26.9) < 0.01, f"{old_read}")

# --- waiting for a foreign prewarm, not its own measurement model (reproducing error from 0.3.6 run 1) ---
class LoadingNow:
    def __init__(self):
        self.loading = {"qwen3.6:35b-a3b": (26.4, time.time() + 300), "gemma4:26b": (19.7, time.time() + 300)}


async def await_preload():
    k = LoadingNow()

    async def prewarm_done():
        await asyncio.sleep(1.0)
        k.loading.pop("qwen3.6:35b-a3b")   # finish_load of the prewarm
    t = asyncio.create_task(prewarm_done())
    t0 = time.time()
    await perf._others_loading(k, "gemma4:26b", timeout=10)
    await t
    return time.time() - t0


waited = asyncio.run(await_preload())
check("Measurement waits for a foreign prewarm, not for its own claim", 0.9 <= waited < 3, f"{waited:.1f}s")

print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
