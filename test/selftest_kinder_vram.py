#!/usr/bin/env python3
"""Selbsttest (Router 0.3.8 / Agent 0.13.0): der Agent meldet den Grafikspeicher seiner Kinder ausser Ollama
(children_vram_mib), der Router zaehlt ihn nicht als fremd und lernt ihn nicht in die Grundlast.

Werte vom 2026-09-30 auf gpu-desktop: belegt 8,2 GiB, kein Modell geladen, Grundlast 2,0 GiB, Whisper per CUDA 2,53 GiB
(Leistungsindikator, 2587 MiB), dazu ein Spiel. Ohne die Meldung: fremd 6,2 GiB >= 6,0 -> busy (foreign_vram), obwohl
Desktop + Spiel nur 3,6 GiB sind. Laeuft ohne Router, Netz und Ollama (echte Config aus router/config.example.yaml)."""
import os
import sys
import time

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "router"))
from skirnir_router import config, nodes, poll, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HIER, "..", "router", "config.example.yaml"), use_overrides=False)
poll.evaluate = lambda node, now: None   # der Zustandsautomat ist hier nicht Gegenstand


def knoten():
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "foreign_vram_baseline_gib": 2.0})
    n.state, n.hb_ts = "free", time.time()
    return n


HB = {"gpu_util_pct": 25, "vram_total_mib": 32607, "vram_used_mib": int(8.2 * 1024), "vram_free_mib": 32607 - int(8.2 * 1024)}

n = knoten()
poll.apply_heartbeat(n, dict(HB), time.time())
check("ohne Meldung (Agent < 0.13): Whisper zaehlt als fremd (6,2 GiB)", abs(n.foreign_vram_gib() - 6.2) < 0.05, f"{n.foreign_vram_gib():.2f}")
check("ohne Meldung: children_vram_gib 0", n.children_vram_gib == 0.0)

n = knoten()
poll.apply_heartbeat(n, dict(HB, children_vram_mib={"stt": 2587}), time.time())
check("mit Meldung: Kinder 2,53 GiB erkannt", abs(n.children_vram_gib - 2587 / 1024) < 0.01, f"{n.children_vram_gib:.2f}")
check("mit Meldung: fremd nur noch Desktop-Rest + Spiel (3,67 GiB)", abs(n.foreign_vram_gib() - (8.2 - 2587 / 1024 - 2.0)) < 0.05,
      f"{n.foreign_vram_gib():.2f}")
check("mit Meldung: unter der foreign-Schwelle (kein busy nur wegen STT)", n.foreign_vram_gib() < n.busy_foreign_threshold(),
      f"{n.foreign_vram_gib():.2f} < {n.busy_foreign_threshold()}")

poll.apply_heartbeat(n, dict(HB), time.time())
check("Kind beendet (Feld fehlt wieder) -> 0", n.children_vram_gib == 0.0)
poll.apply_heartbeat(n, dict(HB, children_vram_mib={"stt": "kaputt", "x": 100}), time.time())
check("unsinnige Werte werden uebergangen", abs(n.children_vram_gib - 100 / 1024) < 0.001, f"{n.children_vram_gib}")

# Grundlast lernen: gelernt wird belegt minus Kinder, sonst zoege foreign_vram_gib sie ein zweites Mal ab
n = knoten()
gesehen = []
n.learn_baseline = lambda now, sample: gesehen.append(sample)
n.polled_ok = time.time()
poll.apply_heartbeat(n, dict(HB, gpu_util_pct=1, vram_used_mib=int(4.6 * 1024), children_vram_mib={"stt": 2587}), time.time())
check("Grundlast lernt ohne den Kinder-Speicher (4,6 - 2,53 = 2,07 GiB)", gesehen and abs(gesehen[-1] - (4.6 - 2587 / 1024)) < 0.05,
      str(gesehen))
check("Knotenzustand zeigt children_vram_gib", n.snapshot(time.time()).get("children_vram_gib") == round(2587 / 1024, 2))

print(f"\n{'ALLE GRUEN' if not FAILS else f'{len(FAILS)} FEHLER: {FAILS}'}")
sys.exit(1 if FAILS else 0)
