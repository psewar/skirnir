#!/usr/bin/env python3
"""Selbsttest Katalog: config.yaml + roles.yaml je Modell Feld fuer Feld (config.merge_models). Ohne Router, ohne Netz.

Anlass 2026-10-02: ein roles.yaml-Eintrag (Messung aus der UI) ersetzte den config.yaml-Eintrag ganz; max_parallel 1 fuer
qwen3.8 stand in config.yaml und galt nicht. Groessenangaben gehoeren aber zusammen: neue Gewichte duerfen nicht mit einer alten
Messung (real, vram_real_gib) gemischt werden.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router.config import merge_models   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


BASIS = {
    "qwen3.8:27b": {"weights_gib": 17.86, "kv_gib_per_1k": 0.0641, "real": True, "vram_gib_8k": 18.37, "max_parallel": 1,
                    "capabilities": {"vision": True}, "note": "aus config.yaml"},
    "gemma4:12b": {"weights_gib": 8.3, "kv_gib_per_1k": 0.0172, "real": True},
}
MESSUNG = {"weights_gib": 17.76, "kv_gib_per_1k": 0.0404, "real": True, "vram_real_gib": {"196608": 25.7}}

basis = copy.deepcopy(BASIS)
m = merge_models(basis, {"qwen3.8:27b": MESSUNG})["qwen3.8:27b"]
check("Messung aus roles.yaml ersetzt die Groessenangaben als Einheit", m["weights_gib"] == 17.76 and m["kv_gib_per_1k"] == 0.0404
      and m["vram_real_gib"] == {"196608": 25.7}, str(m))
check("... alte Messwerte der Basis fallen weg (keine Mischung)", "vram_gib_8k" not in m, str(m))
check("... max_parallel, capabilities, note aus config.yaml bleiben", m.get("max_parallel") == 1 and m.get("capabilities") == {"vision": True}
      and m.get("note") == "aus config.yaml", str(m))
m = merge_models(basis, {"qwen3.8:27b": {"max_parallel": 2}})["qwen3.8:27b"]
check("Overlay ohne Groessenangaben: Basis-Groessen bleiben, Feld wird ueberschrieben", m["weights_gib"] == 17.86
      and m["vram_gib_8k"] == 18.37 and m["max_parallel"] == 2, str(m))
out = merge_models(basis, {"neu:7b": {"weights_gib": 5.0}})
check("neues Modell aus roles.yaml kommt dazu, die anderen bleiben", out["neu:7b"] == {"weights_gib": 5.0} and "gemma4:12b" in out)
check("Basis wird nicht veraendert (Kopie)", basis == BASIS)
check("Overlay ohne Mapping (kaputter Eintrag) ersetzt wie bisher", merge_models(basis, {"gemma4:12b": None})["gemma4:12b"] is None)

print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
