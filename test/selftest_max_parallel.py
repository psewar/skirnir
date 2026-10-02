#!/usr/bin/env python3
"""Selbsttest Grenze je Modell (Katalog max_parallel) und Wartebudget je Prioritaetsklasse. Ohne Router, ohne Netz.

Anlass 2026-10-02: qwen3.8 (Architektur qwen35) kann in Ollama keine zwei Anfragen zugleich. Schickte der Router eine zweite,
stellte Ollama sie in seine Schlange und lud den Runner neu, sobald die erste fertig war (zweimal um 03:02, je 5-7 s). Der
Router muss die zweite selbst zurueckhalten - und beim Freiwerden den Wartenden wecken, der auch wirklich loslegen kann.
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


class Knoten:
    def __init__(self, name, max_inflight=4):
        self.name, self.max_inflight = name, max_inflight
        self.inflight = 0
        self.inflight_models = Counter()

    def effective_max_inflight(self):
        return self.max_inflight


def t_saturated():
    n = Knoten("gpu-desktop")
    check("leer: nicht voll", not admission.saturated(n, "qwen3.8:27b"))
    n.inflight, n.inflight_models["qwen3.8:27b"] = 1, 1
    check("qwen3.8 laeuft einmal, max_parallel 1 -> fuer qwen3.8 voll", admission.saturated(n, "qwen3.8:27b"))
    check("... fuer ein anderes Modell ohne Grenze nicht voll", not admission.saturated(n, "gemma4:12b"))
    check("... ohne Modellangabe (alte Aufrufer) nur die Knotengrenze", not admission.saturated(n))
    n.inflight = 4
    check("Knotengrenze gilt weiter fuer alle Modelle", admission.saturated(n, "gemma4:12b"))


def t_wartebudget():
    check("Wartebudget interactive / normal / batch",
          (admission.max_wait_s("interactive"), admission.max_wait_s("normal"), admission.max_wait_s("batch")) == (60, 600, 1800))


async def t_release():
    """Zwei Wartende auf einem Knoten: einer wartet auf das voll belegte qwen3.8, der andere auf gemma4. Wird ein Platz frei,
    darf nur der geweckt werden, der jetzt loslegen kann - auch wenn der andere besser platziert ist."""
    n = Knoten("gpu-desktop")
    n.inflight, n.inflight_models["qwen3.8:27b"] = 1, 1
    import time
    t = time.time()
    a = asyncio.create_task(admission.wait_for_slot(n, "interactive", t - 100, t + 5, "r-qwen", "code", "qwen3.8:27b"))
    b = asyncio.create_task(admission.wait_for_slot(n, "batch", t, t + 5, "r-gemma", "standard", "gemma4:12b"))
    await asyncio.sleep(0.05)
    admission.release(n)
    await asyncio.sleep(0.05)
    check("release weckt den Wartenden, der loslegen kann (gemma4), nicht den besser platzierten auf vollem qwen3.8",
          b.done() and b.result() is True and not a.done(), f"a {a.done()} b {b.done()}")
    n.inflight, n.inflight_models["qwen3.8:27b"] = 0, 0
    admission.release(n)
    await asyncio.sleep(0.05)
    check("nach Ende der qwen3.8-Anfrage wird der qwen3.8-Wartende geweckt", a.done() and a.result() is True)
    check("Warteschlange danach leer", not admission.WAITING, str(admission.WAITING))


if __name__ == "__main__":
    state.CFG = Cfg()
    t_saturated()
    t_wartebudget()
    asyncio.run(t_release())
    print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
    sys.exit(1 if FAILS else 0)
