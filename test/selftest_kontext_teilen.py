#!/usr/bin/env python3
"""Selbsttest (Router 0.3.7): eine Rollen-Anfrage mit kleinerem num_ctx nimmt den Kontext, mit dem das Modell schon geladen
ist - sofern er innerhalb der Stufe liegt. Anlass 2026-09-30: qwen3.8:27b teilt den Runner nicht ueber verschiedene num_ctx
(anders als qwen3.6); ein Client mit 65536 neben einem mit 131072 lud bei jedem Wechsel neu (10,4 s / 7,4 s gemessen).
Laeuft ohne Router, Netz und Ollama."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import proxy, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class Knoten:
    def __init__(self, geladen):
        self.geladen, self.state = geladen, "free"

    def loaded_context(self, model):
        return self.geladen.get(model)


class Cfg:
    roles = {"assist:latest": {}}
    keep_alive = {"free": -1}


state.CFG = Cfg()
ROLLE = {"exposed": "assist:latest"}
KONKRET = {"exposed": "qwen3.8:27b"}
STUFE = {"model": "qwen3.8:27b", "num_ctx": 131072}
BODY = {"model": "assist:latest", "options": {"num_ctx": 65536}}

out, ctx = proxy._backend_body(BODY, ROLLE, STUFE, 65536, Knoten({"qwen3.8:27b": 131072}))
check("Rolle, Modell mit 131k geladen, Client 65k -> 131k (kein Neuladen)", ctx == 131072 and out["options"]["num_ctx"] == 131072, ctx)

out, ctx = proxy._backend_body(BODY, ROLLE, STUFE, 65536, Knoten({}))
check("Rolle, Modell nicht geladen -> Client-Kontext 65k (kein unnoetiges VRAM)", ctx == 65536, ctx)

out, ctx = proxy._backend_body(BODY, ROLLE, STUFE, 65536, Knoten({"qwen3.8:27b": 32768}))
check("Rolle, mit kleinerem Kontext geladen -> 65k (Neuladen noetig und richtig)", ctx == 65536, ctx)

out, ctx = proxy._backend_body(BODY, ROLLE, {"model": "qwen3.8:27b", "num_ctx": 65536}, 65536, Knoten({"qwen3.8:27b": 131072}))
check("Rolle, geladen groesser als die Stufe erlaubt -> Stufe bleibt Obergrenze (65k)", ctx == 65536, ctx)

out, ctx = proxy._backend_body({"model": "qwen3.8:27b", "options": {"num_ctx": 65536}}, KONKRET,
                               {"model": "qwen3.8:27b", "num_ctx": 65536}, 65536, Knoten({"qwen3.8:27b": 131072}))
check("konkretes Modell mit eigenem num_ctx -> bleibt beim Client-Wunsch", ctx == 65536, ctx)

out, ctx = proxy._backend_body(BODY, ROLLE, STUFE, 131072, Knoten({"qwen3.8:27b": 131072}))
check("Rolle, gleicher Kontext -> unveraendert", ctx == 131072, ctx)

print(f"\n{'ALLE GRUEN' if not FAILS else f'{len(FAILS)} FEHLER: {FAILS}'}")
sys.exit(1 if FAILS else 0)
