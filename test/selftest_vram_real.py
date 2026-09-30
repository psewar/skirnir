#!/usr/bin/env python3
"""Selbsttest der echten VRAM-Anrechnung (Router 0.3.5). Laeuft ohne Router und ohne Netz, mit den echten Klassen Node und
Config, aber ohne Ollama.

Messwerte vom 2026-09-30 auf gpu-desktop (RTX 5090, 31,8 GiB; je Lauf alles entladen, Zuwachs laut nvidia-smi, Desktop
leer 0,6 GiB): qwen3.6:35b-a3b belegt real 24,93 / 26,43 / 27,93 GiB bei 131k / 196k / 262k, /api/ps meldet nur
20,79 / 20,98 / 21,16. laguna-xs-2.1 real 29,71 GiB bei 131k (ps 19,23), bei 196k teilweise im RAM.

Die Gegenproben zeigen, was mit der alten Rechnung (Katalog und Anrechnung aus /api/ps) schiefging: der Wechsel
131k -> 196k desselben Modells passte real, der Router lehnte ihn ab; und der ungemeldete Rest erschien als fremdes VRAM.
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
check("laguna: ausgelagerter Punkt zaehlt mit dem RAM-Anteil (kv > 0)", lag["kv_gib_per_1k"] > 0.01, str(lag))
# 0.3.5 liess den ausgelagerten Punkt weg: granite4.2:30b (8k passt, 32k teilweise im RAM) stand mit flachen 21 GiB da
gra = perf.catalog_entry({8192: (21.21, 20.72, 20.72), 32768: (29.46, 26.1, 28.98)}, "gpu-desktop", real=True)
bedarf32 = gra["weights_gib"] + gra["kv_gib_per_1k"] * 32.768
check("granite-Fall: Bedarf bei 32k >= GPU-Teil + RAM-Teil (29,46 + 2,88)", bedarf32 >= 32.3, f"{bedarf32:.2f} {gra}")

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
check("gemessen: Bedarf ohne Fit-Zuschlag (26,43 bei 196k)", abs(need196 - 26.43) < 0.05, f"{need196:.2f}")
check("ungemessen: Fit-Zuschlag bleibt", abs(Cfg(ALT).need_gib(QWEN, 196608) - (20.6 + 0.001 * 196.608 + 0.8)) < 0.01)
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

# --- Grundlast erst, wenn der Speicher wirklich frei ist (Fehler aus 0.3.5 nachgestellt) ---
class Uhr:
    """Virtuelle Zeit fuer perf: sie laeuft nur, wenn perf schlaeft. Bis 2026-09-30 lief der Nachbau auf der Wanduhr
    (Heartbeat 0,2 s, Abfrage 0,5 s, Karte 1,0 s voll): ob die Abfrage bei ~1,0 s noch den vollen oder schon den leeren
    Heartbeat sah, entschied die Timer-Aufloesung von Windows - die Gegenprobe las in 2 von 3 Laeufen 0,9."""

    def __init__(self, t0=1000.0):
        self.t = t0

    def time(self):
        return self.t

    async def sleep(self, s):
        self.t += s
        await asyncio.sleep(0)


class LangsamFrei:
    """Heartbeat alle 2 s (Agent-Takt), abgeleitet aus der virtuellen Uhr. Entladen bei t0 (/api/ps ist da schon leer),
    die Karte bleibt 5 s voll (26,9 GiB) und faellt erst dann auf 0,9 GiB. Nach dem Entladen kommen also zwei gleiche
    Heartbeats (t0+2, t0+4) bei noch voller Karte - genau das, was 0.3.5 fuer "ruhig" hielt."""

    TAKT, VOLL_FUER = 2.0, 5.0

    def __init__(self, uhr):
        self.uhr, self.t0 = uhr, uhr.time()

    @property
    def hb_ts(self):
        return self.t0 + (self.uhr.time() - self.t0) // self.TAKT * self.TAKT

    @property
    def vram_used_gib(self):
        return 26.9 if self.hb_ts - self.t0 < self.VOLL_FUER else 0.9


def mit_uhr(lesen):
    """`lesen(karte, uhr)` gegen eine frisch entladene Karte; perf.time und perf.asyncio.sleep laufen auf der Uhr."""
    uhr = Uhr()

    class Asyncio:
        sleep = staticmethod(uhr.sleep)

        def __getattr__(self, name):
            return getattr(asyncio, name)

    perf.time, perf.asyncio = uhr, Asyncio()
    try:
        return asyncio.run(lesen(LangsamFrei(uhr), uhr))
    finally:
        perf.time, perf.asyncio = time, asyncio


state.CFG.vram_settle_s = 0   # das Warten aufs Freiwerden allein muss reichen, nicht der Nachlauf
leer_gelesen = mit_uhr(lambda k, uhr: perf._empty_used(k, before=26.9, unloaded=21.0, timeout=60))
check("Grundlast wird erst nach dem Freiwerden gelesen (0,9 statt 26,9 GiB)", abs(leer_gelesen - 0.9) < 0.01, f"{leer_gelesen}")
alt_gelesen = mit_uhr(lambda k, uhr: perf._stable_used(k, uhr.time(), timeout=60))   # 0.3.5: zwei gleiche Heartbeats
check("Gegenprobe: der Weg aus 0.3.5 liest die noch volle Karte (26,9)", abs(alt_gelesen - 26.9) < 0.01, f"{alt_gelesen}")

# --- auf ein fremdes Vorladen warten, das eigene Messmodell nicht (Fehler aus 0.3.6-Lauf 1 nachgestellt) ---
class LaedtGerade:
    def __init__(self):
        self.loading = {"qwen3.6:35b-a3b": (26.4, time.time() + 300), "gemma4:26b": (19.7, time.time() + 300)}


async def vorladen_abwarten():
    k = LaedtGerade()

    async def prewarm_fertig():
        await asyncio.sleep(1.0)
        k.loading.pop("qwen3.6:35b-a3b")   # finish_load des prewarm
    t = asyncio.create_task(prewarm_fertig())
    t0 = time.time()
    await perf._others_loading(k, "gemma4:26b", timeout=10)
    await t
    return time.time() - t0


gewartet = asyncio.run(vorladen_abwarten())
check("Messung wartet auf ein fremdes Vorladen, nicht auf den eigenen Anspruch", 0.9 <= gewartet < 3, f"{gewartet:.1f}s")

print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
