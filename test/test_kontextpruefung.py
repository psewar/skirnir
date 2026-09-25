#!/usr/bin/env python3
"""Selbsttest der Kontext-Vorabpruefung (kontextpruefung.py). Laeuft ohne Router und ohne Netz.

Wichtiger als die Treffer sind die Nicht-Treffer: kleine Anfragen duerfen gar nicht erst geschaetzt werden, und eine
Anfrage, bei der nur die Schaetzung uebertrieben hat (angekommen ~ geschaetzt), darf keinen Alarm ausloesen.
Mit Messdatei (SKIRNIR_TOKENMESSUNG = JSONL mit je `echt`, `bytes`, `ziffern`, `hermes` = Bytes/4-Schaetzung; echte
Tokenzahl vom Modell) wird die Regel zusaetzlich an echten Zaehlwerten nachgerechnet - inklusive Gegenprobe, dass
Bytes/4 dort durchfaellt. Ohne die Variable entfaellt dieser Teil.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from ollama_router import kontextpruefung as kp   # noqa: E402
from ollama_router import state   # noqa: E402

FAILS = []
MESSUNG = os.environ.get("SKIRNIR_TOKENMESSUNG", "")


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def zuruecksetzen():
    state.KUERZUNGEN.update(anzahl=0, letzte=None)
    state.MQTT_EVENTS.clear()
    state.DECISIONS.clear()


PROSA = "Am Montag kontrollierte der Hausmeister die Leitungen, und alles war in Ordnung. "
ZAHLEN = "23.09. 22:41 Uhr Raum Keller Messwert 412.7 Geraet S-042\n"

# --- Schaetzregel ---
check("leer = 0", kp.schaetze_text("") == 0 and kp.schaetze_text(None) == 0)
check("Ziffern je 1 Token", kp.schaetze_text("1234567890") == 10)
p, z = kp.schaetze_text(PROSA * 100), kp.schaetze_text(ZAHLEN * 100)
check("Prosa ~ Bytes/3,5", abs(p - len((PROSA * 100).encode()) / 3.5) <= 1, str(p))
check("Zahlenzeilen teurer als Bytes/4", z > len((ZAHLEN * 100).encode()) / 4 * 1.3, f"{z} vs {len((ZAHLEN * 100).encode()) / 4:.0f}")
check("Nicht-Text wird als JSON gezaehlt", kp.schaetze_text([{"a": 1}]) > 0)

# --- vorher ---
klein = {"messages": [{"role": "user", "content": "Hallo"}]}
check("kleine Anfrage: None", kp.vorher(klein, 131072) is None)
check("ohne ctx: None", kp.vorher({"messages": [{"role": "user", "content": ZAHLEN * 5000}]}, None) is None)
check("Cloud: None", kp.vorher({"messages": [{"role": "user", "content": ZAHLEN * 5000}]}, 1000, is_cloud=True) is None)
gross = {"messages": [{"role": "system", "content": "Du bist ein Agent."}] +
         [{"role": "user" if i % 2 else "assistant", "content": ZAHLEN * 400} for i in range(20)],
         "tools": [{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}]}
est = kp.vorher(gross, 65536)
check("grosser Verlauf ueber num_ctx: Schaetzung", est is not None and est > 65536, str(est))
check("gleicher Verlauf passt in 1M: None", kp.vorher(gross, 1_000_000) is None)
check("/api/generate-Prompt zaehlt", (kp.vorher({"prompt": PROSA * 5000}, 65536) or 0) > 65536)

# --- nachher ---
zuruecksetzen()
info = {"role": "nacht", "model": "qwen3.6:35b-a3b", "node": "gpu-desktop", "client": "agent-x", "path": "/api/chat", "request_id": "r1"}
check("Schaetzung zu hoch, angekommen ~ geschaetzt: kein Alarm", kp.nachher(100_000, 95_000, 131072, info) is False)
check("... und nichts gezaehlt", state.KUERZUNGEN["anzahl"] == 0 and not state.MQTT_EVENTS)
check("angekommen deutlich weniger: bestaetigt", kp.nachher(160_000, 65_538, 131072, info) is True)
check("Zaehler 1", state.KUERZUNGEN["anzahl"] == 1)
l = state.KUERZUNGEN["letzte"] or {}
check("letzte Kuerzung mit Details", l.get("geschaetzt") == 160_000 and l.get("angekommen") == 65_538 and l.get("client") == "agent-x"
      and l.get("num_ctx") == 131072 and l.get("ts"), str(l))
check("HA-Ereignis vorgemerkt", state.MQTT_EVENTS and state.MQTT_EVENTS[-1]["event_type"] == "kontext_gekuerzt")
check("Journal-Eintrag", any(d.get("event") == "kontext_gekuerzt" for d in state.DECISIONS))
check("65538 von 160000 (41 %) gilt als sicher", l.get("sicher") is True and l.get("quote") == 0.41, str(l.get("quote")))
kp.nachher(100_000, 70_000, 65536, info)
check("70 % gilt nur als wahrscheinlich", state.KUERZUNGEN["letzte"]["sicher"] is False)
check("MQTT sofort", bool(state.MQTT_DIRTY))
check("ohne Tokenzahl (Stream abgebrochen): nichts", kp.nachher(160_000, 0, 131072, info) is False)
check("Grenzfall genau 0,8: kein Alarm", kp.nachher(100_000, 80_000, 65536, info) is False)
for _ in range(30):
    kp.nachher(160_000, 1000, 131072, info)
check("Ereignispuffer begrenzt", len(state.MQTT_EVENTS) <= 20, str(len(state.MQTT_EVENTS)))

# --- Nachrechnung an der echten Messung (nur Zaehlwerte) ---
if os.path.exists(MESSUNG):
    with open(MESSUNG, encoding="utf-8") as f:
        rows = [json.loads(x) for x in f]
    def zu_niedrig(schaetz):
        return sum(r["echt"] / max(1, schaetz(r)) > 1.25 for r in rows)
    regel = zu_niedrig(lambda r: r["ziffern"] + -(-(r["bytes"] - r["ziffern"]) // 3.5))
    alt = zu_niedrig(lambda r: r["hermes"])
    check("Messung: Regel hoechstens 2 von 80 um >25 % zu niedrig", regel <= 2, f"{regel}/{len(rows)}")
    check("Messung: Gegenprobe Bytes/4 faellt durch", alt >= 20, f"{alt}/{len(rows)}")
    quoten = [r["echt"] / (r["ziffern"] + -(-(r["bytes"] - r["ziffern"]) // 3.5)) for r in rows]
    fehl = sum(q < kp.BESTAETIGT for q in quoten)
    check("Messung: 'wahrscheinlich' irrt hoechstens bei 5 von 80 ungekuerzten Texten", fehl <= 5, f"{fehl}/{len(rows)}")
    check("Messung: 'sicher' irrt bei keinem gemessenen Text", min(quoten) > kp.SICHER, f"min {min(quoten):.2f}")
else:
    print("SKIP Messung (keine Messdatei)")

zuruecksetzen()
print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
