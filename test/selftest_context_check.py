#!/usr/bin/env python3
"""Self-test for the context pre-check (context_check.py). Runs without a router and without network.

Misses matter more than hits: small requests must not be estimated at all, and a request where only the estimate was excessive (actual ~ estimated) must not trigger an alarm.
With a measurement file (SKIRNIR_TOKENMESSUNG = JSONL with `echt`, `bytes`, `ziffern`, `hermes` = Bytes/4 estimate; actual token count from the model), the rule is additionally recalculated against real counts - including a counter-test that Bytes/4 fails there. Without the variable, this part is omitted.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import context_check as kp   # noqa: E402
from skirnir_router import state   # noqa: E402

FAILS = []
MEASUREMENT = os.environ.get("SKIRNIR_TOKENMESSUNG", "")


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def reset():
    state.TRUNCATIONS.update(count=0, last=None)
    state.MQTT_EVENTS.clear()
    state.DECISIONS.clear()


PROSE = "Am Montag kontrollierte der Hausmeister die Leitungen, und alles war in Ordnung. "
DIGITS = "23.09. 22:41 Uhr Raum Keller Messwert 412.7 Geraet S-042\n"

# --- Schaetzregel ---
check("empty = 0", kp.estimate_text("") == 0 and kp.estimate_text(None) == 0)
check("Digits per 1 token", kp.estimate_text("1234567890") == 10)
p, z = kp.estimate_text(PROSE * 100), kp.estimate_text(DIGITS * 100)
check("Prosa ~ Bytes/3,5", abs(p - len((PROSE * 100).encode()) / 3.5) <= 1, str(p))
check("Number lines costlier than bytes/4", z > len((DIGITS * 100).encode()) / 4 * 1.3, f"{z} vs {len((DIGITS * 100).encode()) / 4:.0f}")
check("Non-text is counted as JSON", kp.estimate_text([{"a": 1}]) > 0)

# --- vorher ---
small = {"messages": [{"role": "user", "content": "Hallo"}]}
check("small request: None", kp.before_request(small, 131072) is None)
check("no ctx: None", kp.before_request({"messages": [{"role": "user", "content": DIGITS * 5000}]}, None) is None)
check("Cloud: None", kp.before_request({"messages": [{"role": "user", "content": DIGITS * 5000}]}, 1000, is_cloud=True) is None)
large = {"messages": [{"role": "system", "content": "Du bist ein Agent."}] +
         [{"role": "user" if i % 2 else "assistant", "content": DIGITS * 400} for i in range(20)],
         "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}]}
est = kp.before_request(large, 65536)
check("large history over num_ctx: estimate", est is not None and est > 65536, str(est))
check("gleicher Verlauf passt in 1M: None", kp.before_request(large, 1_000_000) is None)
check("/api/generate-Prompt counts", (kp.before_request({"prompt": PROSE * 5000}, 65536) or 0) > 65536)

# --- nachher ---
reset()
info = {"role": "nacht", "model": "qwen3.6:35b-a3b", "node": "gpu-desktop", "client": "agent-x", "path": "/api/chat", "request_id": "r1"}
check("estimate too high, arrived ~ estimated: no alarm", kp.after_request(100_000, 95_000, 131072, info) is False)
check("... and nothing counted", state.TRUNCATIONS["count"] == 0 and not state.MQTT_EVENTS)
check("arrived significantly less: confirmed", kp.after_request(160_000, 65_538, 131072, info) is True)
check("Counter 1", state.TRUNCATIONS["count"] == 1)
l = state.TRUNCATIONS["last"] or {}
check("last truncation with details", l.get("estimated") == 160_000 and l.get("received") == 65_538 and l.get("client") == "agent-x"
      and l.get("num_ctx") == 131072 and l.get("ts"), str(l))
check("HA-Ereignis vorgemerkt", state.MQTT_EVENTS and state.MQTT_EVENTS[-1]["event_type"] == "context_truncated")
check("Journal-Eintrag", any(d.get("event") == "context_truncated" for d in state.DECISIONS))
check("65538 of 160000 (41 %) is considered safe", l.get("certain") is True and l.get("ratio") == 0.41, str(l.get("ratio")))
kp.after_request(100_000, 70_000, 65536, info)
check("70 % is only considered likely", state.TRUNCATIONS["last"]["certain"] is False)
check("MQTT sofort", bool(state.MQTT_DIRTY))
check("no token count (Stream aborted): nothing", kp.after_request(160_000, 0, 131072, info) is False)
check("edge case exactly 0.8: no alarm", kp.after_request(100_000, 80_000, 65536, info) is False)
for _ in range(30):
    kp.after_request(160_000, 1000, 131072, info)
check("Ereignispuffer begrenzt", len(state.MQTT_EVENTS) <= 20, str(len(state.MQTT_EVENTS)))

# --- Recalculation against the actual measurement (counts only) ---
if os.path.exists(MEASUREMENT):
    with open(MEASUREMENT, encoding="utf-8") as f:
        rows = [json.loads(x) for x in f]
    def too_low(estimator):
        return sum(r["echt"] / max(1, estimator(r)) > 1.25 for r in rows)
    rule_misses = too_low(lambda r: r["ziffern"] + -(-(r["bytes"] - r["ziffern"]) // 3.5))
    old = too_low(lambda r: r["hermes"])
    check("measurement: rule at most 2 of 80 by >25 % too low", rule_misses <= 2, f"{rule_misses}/{len(rows)}")
    check("measurement: counter-test Bytes/4 fails", old >= 20, f"{old}/{len(rows)}")
    ratios = [r["echt"] / (r["ziffern"] + -(-(r["bytes"] - r["ziffern"]) // 3.5)) for r in rows]
    misses = sum(q < kp.CONFIRMED for q in ratios)
    check("measurement: 'likely' errs at most in 5 of 80 untruncated texts", misses <= 5, f"{misses}/{len(rows)}")
    check("measurement: 'certain' errs in no measured text", min(ratios) > kp.CERTAIN, f"min {min(ratios):.2f}")
else:
    print("SKIP measurement (no measurement file)")

reset()
print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
