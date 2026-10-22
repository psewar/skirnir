#!/usr/bin/env python3
"""Self-test catalog: config.yaml + roles.yaml per model field by field (config.merge_models). No router, no network.

Trigger 2026-10-02: a roles.yaml entry (measurement from the UI) completely replaced the config.yaml entry; max_parallel 1 for
qwen3.8 was in config.yaml and did not apply. Size specifications belong together: new weights must not be mixed with an old
measurement (real, vram_real_gib).
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


BASE = {
    "qwen3.8:27b": {"weights_gib": 17.86, "kv_gib_per_1k": 0.0641, "real": True, "vram_gib_8k": 18.37, "max_parallel": 1,
                    "capabilities": {"vision": True}, "note": "aus config.yaml"},
    "gemma4:12b": {"weights_gib": 8.3, "kv_gib_per_1k": 0.0172, "real": True},
}
MEASUREMENT = {"weights_gib": 17.76, "kv_gib_per_1k": 0.0404, "real": True, "vram_real_gib": {"196608": 25.7}}

base = copy.deepcopy(BASE)
m = merge_models(base, {"qwen3.8:27b": MEASUREMENT})["qwen3.8:27b"]
check("Measurement from roles.yaml replaces size specs as a unit", m["weights_gib"] == 17.76 and m["kv_gib_per_1k"] == 0.0404
      and m["vram_real_gib"] == {"196608": 25.7}, str(m))
check("... old base measurements are dropped (no mixing)", "vram_gib_8k" not in m, str(m))
check("... max_parallel, capabilities, note from config.yaml remain", m.get("max_parallel") == 1 and m.get("capabilities") == {"vision": True}
      and m.get("note") == "aus config.yaml", str(m))
m = merge_models(base, {"qwen3.8:27b": {"max_parallel": 2}})["qwen3.8:27b"]
check("Overlay without size specs: base sizes remain, field is overwritten", m["weights_gib"] == 17.86
      and m["vram_gib_8k"] == 18.37 and m["max_parallel"] == 2, str(m))
out = merge_models(base, {"neu:7b": {"weights_gib": 5.0}})
check("new model from roles.yaml is added, others remain", out["neu:7b"] == {"weights_gib": 5.0} and "gemma4:12b" in out)
check("Base is not modified (copy)", base == BASE)
check("Overlay without mapping (broken entry) replaces as before", merge_models(base, {"gemma4:12b": None})["gemma4:12b"] is None)

print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
