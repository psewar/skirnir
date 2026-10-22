#!/usr/bin/env python3
"""Self-test (Router 0.3.8 / Agent 0.13.0): the agent reports the GPU memory of its children except Ollama
(children_vram_mib); the router does not count it as foreign and does not learn it into the baseline.

Values from 2026-09-30 on gpu-desktop: busy 8.2 GiB, no model loaded, baseline 2.0 GiB, Whisper via CUDA 2.53 GiB
(performance indicator, 2587 MiB), plus a game. Without the report: foreign 6.2 GiB >= 6.0 -> busy (foreign_vram), even though
Desktop + game are only 3.6 GiB. Runs without Router, network, and Ollama (real config from router/config.example.yaml)."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, poll, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
poll.evaluate = lambda node, now: None   # the state machine is not the subject here


def add_node():
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "foreign_vram_baseline_gib": 2.0})
    n.state, n.hb_ts = "free", time.time()
    return n


HB = {"gpu_util_pct": 25, "vram_total_mib": 32607, "vram_used_mib": int(8.2 * 1024), "vram_free_mib": 32607 - int(8.2 * 1024)}

n = add_node()
poll.apply_heartbeat(n, dict(HB), time.time())
check("no report (Agent < 0.13): Whisper counts as foreign (6.2 GiB)", abs(n.foreign_vram_gib() - 6.2) < 0.05, f"{n.foreign_vram_gib():.2f}")
check("no report: children_vram_gib 0", n.children_vram_gib == 0.0)

n = add_node()
poll.apply_heartbeat(n, dict(HB, children_vram_mib={"stt": 2587}), time.time())
check("with report: children 2.53 GiB detected", abs(n.children_vram_gib - 2587 / 1024) < 0.01, f"{n.children_vram_gib:.2f}")
check("with report: foreign only Desktop remainder + game (3.67 GiB)", abs(n.foreign_vram_gib() - (8.2 - 2587 / 1024 - 2.0)) < 0.05,
      f"{n.foreign_vram_gib():.2f}")
check("with report: below foreign threshold (no busy solely due to STT)", n.foreign_vram_gib() < n.busy_foreign_threshold(),
      f"{n.foreign_vram_gib():.2f} < {n.busy_foreign_threshold()}")

poll.apply_heartbeat(n, dict(HB), time.time())
check("Child terminated (field missing again) -> 0", n.children_vram_gib == 0.0)
poll.apply_heartbeat(n, dict(HB, children_vram_mib={"stt": "kaputt", "x": 100}), time.time())
check("nonsensical values are ignored", abs(n.children_vram_gib - 100 / 1024) < 0.001, f"{n.children_vram_gib}")

# Baseline learning: learned value is busy minus children, otherwise foreign_vram_gib would deduct it a second time
n = add_node()
seen = []
n.learn_baseline = lambda now, sample: seen.append(sample)
n.polled_ok = time.time()
poll.apply_heartbeat(n, dict(HB, gpu_util_pct=1, vram_used_mib=int(4.6 * 1024), children_vram_mib={"stt": 2587}), time.time())
check("baseline learns without child memory (4.6 - 2.53 = 2.07 GiB)", seen and abs(seen[-1] - (4.6 - 2587 / 1024)) < 0.05,
      str(seen))
check("Knotenzustand zeigt children_vram_gib", n.snapshot(time.time()).get("children_vram_gib") == round(2587 / 1024, 2))

print(f"\n{'ALLE GRUEN' if not FAILS else f'{len(FAILS)} FEHLER: {FAILS}'}")
sys.exit(1 if FAILS else 0)
