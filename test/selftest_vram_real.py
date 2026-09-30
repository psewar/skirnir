#!/usr/bin/env python3
"""Selbsttest der echten VRAM-Anrechnung (Router 0.3.5). Laeuft ohne Router und ohne Netz, mit den echten Klassen Node und
Config, aber ohne Ollama.

Messwerte vom 2026-09-30 auf gpu-desktop (RTX 5090, 31,8 GiB; je Lauf alles entladen, Zuwachs laut nvidia-smi, Desktop
leer 0,6 GiB): qwen3.6:35b-a3b belegt real 24,93 / 26,43 / 27,93 GiB bei 131k / 196k / 262k, /api/ps meldet nur
20,79 / 20,98 / 21,16. laguna-xs-2.1 real 29,71 GiB bei 131k (ps 19,23), bei 196k teilweise im RAM.

Die Gegenproben zeigen, was mit der alten Rechnung (Katalog und Anrechnung aus /api/ps) schiefging: der Wechsel
131k -> 196k desselben Modells passte real, der Router lehnte ihn ab; und der ungemeldete Rest erschien als fremdes VRAM.
"""
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
GEMESSEN = {131072: (24.93, 20.79, 20.79), 196608: (26.43, 20.98, 20.98), 262144: (27.93, 21.16, 21.16)}


class Cfg(config.Config):
    """Nur was need_gib/real_gib/Node brauchen - ohne Datei."""

    def __init__(self, models, roles=None):
        self.models = models
        self.roles = roles or {}
        self.overhead_gib = 0.8
        self.reserve = {"free": 1.0, "busy": 2.0}
        self.hb_stale_s = 10
        self.vram_settle_s = 8


def knoten(used, loaded_ctx=131072, ps=20.79, baseline=1.0):
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "foreign_vram_baseline_gib": baseline})
    n.state = "free"
    n.hb_ts = time.time()
    n.vram_used_gib, n.vram_free_gib = used, 31.8 - used
    n.digest_of = {QWEN: "sha-qwen"}
    n.loaded = {QWEN: ps}
    n.loaded_digest = {"sha-qwen": ps}
    n.loaded_ctx = {"sha-qwen": loaded_ctx}
    return n


# --- Ausgleichsgerade aus den echten Punkten ---
w, kv = perf.fit_linear([(c, v[0]) for c, v in GEMESSEN.items()])
check("Gerade: kv 0,0229 GiB/1k", abs(kv - 0.0229) < 0.0005, f"{kv:.4f}")
check("Gerade: Grundwert 21,93 GiB", abs(w - 21.93) < 0.05, f"{w:.2f}")
e = perf.catalog_entry(GEMESSEN, "gpu-desktop", real=True)
check("Katalog-Eintrag markiert real + haelt beide Messreihen", e["real"] is True and e["vram_real_gib"]["196608"] == 26.43
      and e["vram_ps_gib"]["196608"] == 20.98 and e["source"] == "gemessen (nvidia-smi)", str(e))

# --- Teil-Auslagerung zaehlt nicht fuer die Gerade ---
lag = perf.catalog_entry({8192: (27.0, 19.0, 19.0), 32768: (27.6, 19.1, 19.1), 131072: (29.71, 19.23, 19.23),
                          196608: (29.65, 14.17, 19.77)}, "gpu-desktop", real=True)
check("laguna: 196k als partial_offload gemeldet", lag.get("partial_offload") == [196608], str(lag.get("partial_offload")))
check("laguna: Gerade ohne den ausgelagerten Punkt (kv > 0)", lag["kv_gib_per_1k"] > 0.01, str(lag))

# --- Messpunkte: fest + jeder Kontext der Rollen ---
state.CFG = Cfg({}, {"nacht:latest": {"tiers": [{"model": QWEN, "num_ctx": 196608}]},
                     "assist:latest": {"tiers": [{"model": QWEN, "num_ctx": 196608}, {"model": QWEN, "num_ctx": 32768}]},
                     "cloud:latest": {"tiers": [{"model": "openai:gpt-5-mini", "num_ctx": 128000, "cloud": "openai"}]}})
check("Messpunkte: 8k, 32k und 196k der Rollen", perf.measure_ctxs(QWEN) == [8192, 32768, 196608], str(perf.measure_ctxs(QWEN)))

# --- Anrechnung geladener Modelle ---
REAL = {QWEN: {"weights_gib": round(w, 2), "kv_gib_per_1k": round(kv, 4), "real": True}}
ALT = {QWEN: {"weights_gib": 20.6, "kv_gib_per_1k": 0.001}}   # der Katalog bis 0.3.4 (aus /api/ps)
state.NODES.clear()

state.CFG = Cfg(REAL)
n = knoten(used=25.52)
check("real_gib bei 131k = 24,93", abs(state.CFG.real_gib(QWEN, 131072) - 24.93) < 0.05, f"{state.CFG.real_gib(QWEN, 131072):.2f}")
check("geladenes Modell mit echter Belegung angerechnet", abs(n.ollama_vram_gib() - 24.93) < 0.05, f"{n.ollama_vram_gib():.2f}")
need196 = state.CFG.need_gib(QWEN, 196608, n)
budget = n.budget_gib(time.time(), QWEN)
check("Wechsel 131k -> 196k passt (Bedarf <= Budget)", need196 <= budget, f"need {need196:.2f} budget {budget:.2f}")
check("kein Phantom-Fremd-VRAM bei geladenem Modell", n.foreign_vram_gib() < 0.1, f"{n.foreign_vram_gib():.2f}")

state.CFG = Cfg(ALT)
n_alt = knoten(used=25.52)
need_alt = Cfg(REAL).need_gib(QWEN, 196608, n_alt)   # der echte Bedarf, gegen die alte Anrechnung
budget_alt = n_alt.budget_gib(time.time(), QWEN)
check("Gegenprobe: alte Anrechnung lehnt den realen Wechsel ab", need_alt > budget_alt, f"need {need_alt:.2f} budget {budget_alt:.2f}")
check("Gegenprobe: alter Rest erscheint als fremdes VRAM", n_alt.foreign_vram_gib() > 3.0, f"{n_alt.foreign_vram_gib():.2f}")
check("ohne real-Messung bleibt es bei /api/ps (rueckwaertskompatibel)", abs(n_alt.ollama_vram_gib() - 20.79) < 0.01)

# --- Spiel ohne geladenes Modell: mit realistischer Grundlast voll sichtbar ---
state.CFG = Cfg(REAL)
leer = knoten(used=11.04, baseline=1.0)
leer.loaded, leer.loaded_digest, leer.loaded_ctx = {}, {}, {}
check("Spiel ohne Modell: 10 GiB fremd (Grundlast 1,0)", abs(leer.foreign_vram_gib() - 10.04) < 0.05, f"{leer.foreign_vram_gib():.2f}")
leer6 = knoten(used=11.04, baseline=6.0)
leer6.loaded, leer6.loaded_digest, leer6.loaded_ctx = {}, {}, {}
check("Gegenprobe: mit der alten Grundlast 6,0 nur 5 GiB (so am 2026-09-29)", abs(leer6.foreign_vram_gib() - 5.04) < 0.05,
      f"{leer6.foreign_vram_gib():.2f}")

# --- UI speichert den Katalog: gemessene Eintraege bleiben ---
cur = {"models": {QWEN: {"weights_gib": 21.93, "kv_gib_per_1k": 0.0229, "real": True, "vram_real_gib": {"196608": 26.43}}}}
gleich = admin._merge_overrides(cur, {"models": {QWEN: {"weights_gib": 21.93, "kv_gib_per_1k": 0.0229}}})
check("UI-Speichern mit gleichen Zahlen behaelt die Messung", gleich["models"][QWEN].get("real") is True
      and gleich["models"][QWEN].get("vram_real_gib") == {"196608": 26.43})
anders = admin._merge_overrides(cur, {"models": {QWEN: {"weights_gib": 22.5, "kv_gib_per_1k": 0.0229}}})
check("UI-Speichern mit geaenderten Zahlen ersetzt (Bediener gewinnt)", anders["models"][QWEN] == {"weights_gib": 22.5, "kv_gib_per_1k": 0.0229})

print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
