#!/usr/bin/env python3
"""Self-test for per-model limits (catalog max_parallel) and wait budgets per priority class. No router, no network.

Trigger 2026-10-02: qwen3.8 (architecture qwen35) cannot handle two concurrent requests in Ollama. If the router sent a second one,
Ollama queued it and reloaded the runner once the first finished (twice at 03:02, 5-7 s each). The
router must hold the second one itself - and wake the waiter when a slot frees up, ensuring they can actually start.
"""
import asyncio
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import admission, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class Cfg:
    admission = {"max_inflight_default": 4, "aging_s": 30, "max_wait_s": 600, "max_queue": 16,
                 "max_wait_interactive_s": 60, "max_wait_batch_s": 1800}
    limits = {"qwen3.8:27b": 1}

    def max_parallel(self, model):
        return self.limits.get(model)


class FakeNode:
    def __init__(self, name, max_inflight=4):
        self.name, self.max_inflight = name, max_inflight
        self.inflight = 0
        self.inflight_models = Counter()

    def effective_max_inflight(self):
        return self.max_inflight


def t_saturated():
    n = FakeNode("gpu-desktop")
    check("empty: not full", not admission.saturated(n, "qwen3.8:27b"))
    n.inflight, n.inflight_models["qwen3.8:27b"] = 1, 1
    check("qwen3.8 runs once, max_parallel 1 -> full for qwen3.8", admission.saturated(n, "qwen3.8:27b"))
    check("... not full for another model without limit", not admission.saturated(n, "gemma4:12b"))
    check("... without model specification (old callers) only the node limit", not admission.saturated(n))
    n.inflight = 4
    check("Node limit still applies to all models", admission.saturated(n, "gemma4:12b"))


def t_wait_budget():
    check("Wartebudget interactive / normal / batch",
          (admission.max_wait_s("interactive"), admission.max_wait_s("normal"), admission.max_wait_s("batch")) == (60, 600, 1800))


async def t_release():
    """Two waiters on one node: one waiting for the fully occupied qwen3.8, the other for gemma4. When a slot frees up,
    only the one that can start now should be woken - even if the other is better positioned."""
    n = FakeNode("gpu-desktop")
    n.inflight, n.inflight_models["qwen3.8:27b"] = 1, 1
    import time
    t = time.time()
    a = asyncio.create_task(admission.wait_for_slot(n, "interactive", t - 100, t + 5, "r-qwen", "code", "qwen3.8:27b"))
    b = asyncio.create_task(admission.wait_for_slot(n, "batch", t, t + 5, "r-gemma", "standard", "gemma4:12b"))
    await asyncio.sleep(0.05)
    admission.release(n)
    await asyncio.sleep(0.05)
    check("release wakes the waiter that can start (gemma4), not the better placed one on full qwen3.8",
          b.done() and b.result() is True and not a.done(), f"a {a.done()} b {b.done()}")
    n.inflight, n.inflight_models["qwen3.8:27b"] = 0, 0
    admission.release(n)
    await asyncio.sleep(0.05)
    check("after the qwen3.8 request ends, the qwen3.8 waiter is woken", a.done() and a.result() is True)
    check("Queue empty afterwards", not admission.WAITING, str(admission.WAITING))


if __name__ == "__main__":
    state.CFG = Cfg()
    t_saturated()
    t_wait_budget()
    asyncio.run(t_release())
    print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
    sys.exit(1 if FAILS else 0)
