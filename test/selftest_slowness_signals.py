#!/usr/bin/env python3
"""Self-test (Router 0.9.2): slowness signals for Home Assistant.

1. A loaded model that Ollama placed partly in system RAM (/api/ps: size > size_vram) is a fact on the node, in the
   HA view, as a problem from `ram_spill_problem_gib`, and as a binary sensor. The case behind it: a configuration change
   dropped the KV cache quantisation, the model no longer fit the card, 3.2 GiB went to system RAM and every request ran
   at 8.8 instead of ~100 tok/s - nothing failed, so it only showed as slow answers.
2. The rates of the last warm request (generation, prompt) reach HA per node, but only above a minimum size, because a
   reply of a few tokens or a cached prompt gives a noisy rate; benchmarks do not count.
Runs without router, network and Ollama."""
import json
import os
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, ha, nodes, perf, poll, state   # noqa: E402
from skirnir_router.common import GIB   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.prewarm_on_online = False
poll.evaluate = lambda node, now: None
state.CAPS["synth-model:27b"] = ["completion", "tools"]
TAG = {"name": "synth-model:27b", "digest": "aa", "size": 17 * GIB}


def ps(size_gib, vram_gib):
    return {"name": "synth-model:27b", "digest": "aa", "size": int(size_gib * GIB), "size_vram": int(vram_gib * GIB),
            "context_length": 196608, "expires_at": "2026-10-22T22:00:00Z"}


check("config default: problem from 0.5 GiB in RAM", state.CFG.ram_spill_problem_gib == 0.5, str(state.CFG.ram_spill_problem_gib))

# --- 1. RAM fact from /api/ps
n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
state.NODES = {n.name: n}
now = time.time()
poll.apply_ollama_push(n, {"up": True, "rev": 1, "tags": [TAG], "ps": [ps(17.7, 17.7)]}, now)
check("fits the card: no RAM spill", n.ram_spill == {}, str(n.ram_spill))

state.MQTT_DIRTY.clear()
poll.apply_ollama_push(n, {"up": True, "rev": 2, "tags": [TAG], "ps": [ps(19.48, 16.27)]}, now + 1)
check("partly in system RAM: 3.21 GiB on the node", abs(n.ram_spill.get("synth-model:27b", 0) - 3.21) < 0.01, str(n.ram_spill))
check("change reaches HA at once (MQTT dirty)", bool(state.MQTT_DIRTY))

poll.apply_ollama_push(n, {"up": True, "rev": 3, "tags": [TAG], "ps": [ps(17.73, 17.70)]}, now + 2)
check("0.03 GiB difference is rounding, not spill", n.ram_spill == {}, str(n.ram_spill))

check("entry without sizes counts as 0", poll.ram_spill_gib({"name": "x"}) == 0.0
      and poll.ram_spill_gib({"name": "x", "size": "big", "size_vram": 1}) == 0.0)

poll.apply_ollama_push(n, {"up": True, "rev": 4, "tags": [TAG], "ps": [ps(19.48, 16.27)]}, now + 3)
n.go_offline("test")
check("offline: RAM fact cleared with the loaded models", n.ram_spill == {} and n.loaded == {})

# --- 2. Rates of real requests
state.MQTT_DIRTY.clear()
n.note_perf("synth-model:27b", {"gen_tps": 101.1, "prompt_tps": 119.0, "tokens": 88, "prompt_tokens": 39}, now)
check("long reply, short prompt: generation rate taken, prompt rate not",
      n.last_gen == {"model": "synth-model:27b", "tps": 101.1, "ts": now} and n.last_prompt is None, f"{n.last_gen} {n.last_prompt}")
check("new rate reaches HA at once (MQTT dirty)", bool(state.MQTT_DIRTY))
state.MQTT_DIRTY.clear()
n.note_perf("synth-model:27b", {"gen_tps": 7.4, "prompt_tps": 276.8, "tokens": 2, "prompt_tokens": 562}, now + 1)
check("2-token reply: generation rate unchanged, long prompt counts",
      n.last_gen["tps"] == 101.1 and n.last_prompt == {"model": "synth-model:27b", "tps": 276.8, "ts": now + 1}, f"{n.last_gen} {n.last_prompt}")
n.note_perf("synth-model:27b", {"gen_tps": 9.0, "prompt_tps": None, "tokens": 5, "prompt_tokens": 10}, now + 2)
check("too small for both: nothing changes, no MQTT update", n.last_gen["tps"] == 101.1 and n.last_prompt["tps"] == 276.8
      and len(state.MQTT_DIRTY) == 1, str(state.MQTT_DIRTY))

STATS = {"eval_count": 160, "eval_duration": int(19.3e9), "prompt_eval_count": 1277, "prompt_eval_duration": int(13.7e9),
         "load_duration": int(8e6)}
state.PERF_DIRTY = [0]
perf.perf_record("synth-model:27b", "gpu-desktop", STATS, 17.6, kind="bench")
check("benchmark run does not reach the HA rate", n.last_gen["tps"] == 101.1, str(n.last_gen))
perf.perf_record("synth-model:27b", "gpu-desktop", STATS, 17.6)
check("passive request reaches the HA rates", n.last_gen["tps"] == 8.3 and n.last_prompt["tps"] == 93.2, f"{n.last_gen} {n.last_prompt}")
perf.perf_record("synth-model:27b", "gpu-desktop", {**STATS, "eval_duration": int(1.6e9), "load_duration": int(7.8e9)}, 17.6)
check("cold run (load > 1.5 s, 100 tok/s) does not count", n.last_gen["tps"] == 8.3, str(n.last_gen))

# --- HA view: problem text and node fields
poll.apply_ollama_push(n, {"up": True, "rev": 5, "tags": [TAG], "ps": [ps(19.48, 16.27)]}, time.time())
snap = ha.ha_snapshot()
v = snap["nodes"]["gpu-desktop"]
check("HA view: RAM GiB, models and rates", v["ram_spill_gib"] == 3.21 and v["ram_spill_models"] == {"synth-model:27b": 3.21}
      and v["gen_tps"] == 8.3 and v["gen_tps_model"] == "synth-model:27b" and v["prompt_tps"] == 93.2, json.dumps(v)[:300])
check("HA problem names node, model and GiB", any("gpu-desktop" in p and "synth-model:27b" in p and "3.2 GiB" in p for p in snap["problems"]),
      str(snap["problems"]))
state.CFG.ram_spill_problem_gib = 0
check("ram_spill_problem_gib 0: no problem, fact stays", not any("Arbeitsspeicher" in p for p in ha.ha_snapshot()["problems"])
      and ha.ha_snapshot()["nodes"]["gpu-desktop"]["ram_spill_gib"] == 3.21)
state.CFG.ram_spill_problem_gib = 5.0
check("below the threshold: no problem", not any("Arbeitsspeicher" in p for p in ha.ha_snapshot()["problems"]))
check("admin view carries the fact", n.snapshot(time.time())["ram_spill_gib"] == {"synth-model:27b": 3.21})


# --- Discovery: three new node entities with English entity IDs
class Client:
    def __init__(self):
        self.pubs = {}

    def publish(self, topic, payload, qos=0, retain=False):
        self.pubs[topic] = payload


cfg_backup, state.CFG = state.CFG, SimpleNamespace(public_url=None)
pub = ha.HAPublisher.__new__(ha.HAPublisher)
pub.client, pub.base, pub.prefix = Client(), "skirnir-router", "homeassistant"
ents = {(c, o): (label, extra) for c, o, label, extra in pub._node_entities("gpu-desktop")}
for comp, oid in (("binary_sensor", "node_gpu_desktop_ram_spill"), ("sensor", "node_gpu_desktop_gen_tps"),
                  ("sensor", "node_gpu_desktop_prompt_tps")):
    check(f"entity {comp}.{oid} exists", (comp, oid) in ents)
label, extra = ents[("binary_sensor", "node_gpu_desktop_ram_spill")]
pub._ent("binary_sensor", "node_gpu_desktop_ram_spill", label, **extra)
k = json.loads(pub.client.pubs["homeassistant/binary_sensor/skirnir_router/node_gpu_desktop_ram_spill/config"])
check("RAM sensor: problem class, English entity ID, attributes", k["device_class"] == "problem"
      and k["default_entity_id"] == "binary_sensor.skirnir_node_gpu_desktop_ram_spill" and "json_attributes_template" in k, str(k)[:200])
label, extra = ents[("sensor", "node_gpu_desktop_gen_tps")]
check("rate sensor: tok/s, measurement, unavailable without a value", extra["unit_of_measurement"] == "tok/s"
      and extra["state_class"] == "measurement" and extra["availability_mode"] == "all")
state.CFG = cfg_backup

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
