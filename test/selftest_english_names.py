#!/usr/bin/env python3
"""Self-test (Router 0.9.0): English interface names and the transition from German (design/english-names.md).

Agents prior to 0.20.0 report GPU guard in German, events.jsonl contains old event and base names, anfragen.jsonl is now requests.jsonl, and HA entities receive an English entity ID with a fixed unique_id. Runs without Router and network."""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "router"))
from skirnir_router import admin, fallback_observation, ha, nodes, state  # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


# --- GPU guard: old agents German, new English, both normalized to English
alt = nodes.guard_english({"state": "gedrosselt", "quelle": "nvml", "grund": "abgewaehlt", "warnungen": ["x"], "hochlast_s": 12,
                           "limit_w": 403})
check("Agent < 0.20: Zustand gedrosselt -> throttled", alt["state"] == "throttled")
check("Agent < 0.20: Felder quelle/grund/warnungen/hochlast_s -> englisch",
      alt.get("source") == "nvml" and alt.get("reason") == "deselected" and alt.get("warnings") == ["x"] and alt.get("high_load_s") == 12
      and not ({"quelle", "grund", "warnungen", "hochlast_s"} & set(alt)), str(alt))
neu = {"state": "high_load", "source": "nvml", "reason": None, "limit_w": 460}
check("Agent >= 0.20: unveraendert", nodes.guard_english(neu) == neu)
check("alle Zustaende englisch", set(nodes.GUARD_STATE_BEFORE_0_20.values()) <= set(nodes.GUARD_STATES))

# --- Journal: old entries translated on load
zeilen = [{"event": "kontext_gekuerzt", "t": 1, "geschaetzt": 100, "angekommen": 40, "quote": 0.4, "sicher": True},
          {"event": "prewarm", "t": 2, "reason": "spiel-leicht"},
          {"event": "gpu_guard", "t": 3, "state": "hochlast", "reason": "abgewaehlt"},
          {"event": "shadow", "t": 4, "status": "cloud uebersprungen"},
          {"event": "route", "t": 5, "reason": "ausweichen"}]
r = [state._journal_row(json.dumps(z)) for z in zeilen]
check("context_truncated mit englischen Feldern", r[0]["event"] == "context_truncated" and r[0]["estimated"] == 100
      and r[0]["received"] == 40 and r[0]["ratio"] == 0.4 and r[0]["certain"] is True and "geschaetzt" not in r[0], str(r[0]))
check("Grund spiel-leicht -> game_light", r[1]["reason"] == "game_light")
check("gpu_guard hochlast/abgewaehlt -> high_load/deselected", r[2]["state"] == "high_load" and r[2]["reason"] == "deselected")
check("Shadow-Status -> cloud_skipped", r[3]["status"] == "cloud_skipped")
check("Route-Grund ausweichen -> fallback", r[4]["reason"] == "fallback")
check("neuer Eintrag unveraendert", state._journal_row('{"event": "route", "t": 6, "reason": "warm-first"}')["reason"] == "warm-first")

# --- anfragen.jsonl -> requests.jsonl
tmp = tempfile.mkdtemp(prefix="skirnir-en-")
state.CFG = SimpleNamespace(path=os.path.join(tmp, "config.yaml"), decision={})
open(os.path.join(tmp, "anfragen.jsonl"), "w").write('{"a": 1}\n')
open(os.path.join(tmp, "anfragen.jsonl.1"), "w").write('{"a": 0}\n')
fallback_observation.adopt_file_before_0_9()
check("anfragen.jsonl und .1 uebernommen", open(os.path.join(tmp, "requests.jsonl")).read() == '{"a": 1}\n'
      and os.path.exists(os.path.join(tmp, "requests.jsonl.1")) and not os.path.exists(os.path.join(tmp, "anfragen.jsonl")))
open(os.path.join(tmp, "anfragen.jsonl"), "w").write("alt\n")
fallback_observation.adopt_file_before_0_9()
check("vorhandenes requests.jsonl wird nicht ueberschrieben", open(os.path.join(tmp, "requests.jsonl")).read() == '{"a": 1}\n')

# --- Catalog source: old German values reported in English
check("Katalogquellen vor 0.9 -> englisch", admin.CATALOG_SOURCE_BEFORE_0_9["gemessen (nvidia-smi)"] == "measured (nvidia-smi)"
      and admin.CATALOG_SOURCE_BEFORE_0_9["geschaetzt"] == "estimated")


# --- HA Discovery: fixed unique_id, English entity ID
class Client:
    def __init__(self):
        self.pubs = {}

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs[topic] = payload


state.CFG = SimpleNamespace(public_url=None)
pub = ha.HAPublisher.__new__(ha.HAPublisher)
pub.client, pub.base, pub.prefix = Client(), "skirnir-router", "homeassistant"
pub._ent("sensor", "kuerzungen", "Gekürzte Anfragen", entity="truncations")
pub._ent("sensor", "nodes_online", "Knoten online")
k = json.loads(pub.client.pubs["homeassistant/sensor/skirnir_router/kuerzungen/config"])
check("Kuerzungen: unique_id bleibt, Entity-ID englisch", k["unique_id"] == "skirnir_router_kuerzungen"
      and k["default_entity_id"] == "sensor.skirnir_truncations" and "object_id" not in k, str(k))
o = json.loads(pub.client.pubs["homeassistant/sensor/skirnir_router/nodes_online/config"])
check("Knoten online: Entity-ID aus der oid", o["default_entity_id"] == "sensor.skirnir_nodes_online" and o["name"] == "Knoten online")

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
