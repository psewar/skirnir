#!/usr/bin/env python3
"""Self-test (Router 0.14.0): sleep and wake-up as a fact from the agent (frame POWER, Agent >= 0.24.0).

Trigger 2026-10-23: a desktop node went to standby, and the router only noticed a ping timeout a minute later; short standby
wake-ups looked like a flapping tunnel. Checks the frame, the node state, the journal, the heartbeat fallback, and that
all of it works without Home Assistant: the HA view and the HA entity are optional extras on top of the fact.
Runs without router process, network or MQTT (real config from router/config.example.yaml)."""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, ha, nodes, poll, state, tunnel   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
state.CFG.mqtt = None   # no Home Assistant: everything below must work without it
state.spawn = lambda coro: coro.close()


def add_node():
    now = time.time()
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
    n.state, n.hb_ts, n.free_at = "free", now, now
    state.NODES = {n.name: n}
    tun = tunnel.Tunnel(n, None, "192.0.2.10")
    tun.approved = True
    return n, tun


def power_frame(msg):
    return tunnel.tun_frame(tunnel.TUN_POWER, 0, json.dumps(msg).encode() if not isinstance(msg, bytes) else msg)


def journal(node):
    return [d for d in state.DECISIONS if d.get("event") == "power" and d.get("node") == node.name]


# 1) Unknown until the agent reports
n, tun = add_node()
check("without a notice the power state is unknown (None), not 'awake'", n.power is None and n.snapshot(time.time())["power"] is None)

# 2) Suspend notice: node leaves routing at once, fact and journal entry
tun.dispatch(power_frame({"state": "suspending", "at": "2026-10-23T18:00:00Z", "via": "PBT_APMSUSPEND"}))
check("suspend -> power asleep, node offline at once", n.power == "asleep" and n.state == "offline", f"{n.power} {n.state}")
check("journal event power/asleep with its source", journal(n)[-1:] and journal(n)[-1]["state"] == "asleep"
      and journal(n)[-1]["via"] == "PBT_APMSUSPEND", str(journal(n)[-1:]))
since = n.power_since
tun.dispatch(power_frame({"state": "suspending", "via": "PBT_APMSUSPEND"}))
check("the same state twice is one journal entry and keeps power_since", len(journal(n)) == 1 and n.power_since == since)

# 3) Resume notice, and heartbeat as fallback when the notice was lost
tun.dispatch(power_frame({"state": "resumed", "via": "PBT_APMRESUMEAUTOMATIC"}))
check("resume -> power awake", n.power == "awake" and len(journal(n)) == 2)
n.power = "asleep"
poll.apply_heartbeat(n, {"gpu_util_pct": 1, "vram_total_mib": 32000, "vram_used_mib": 1000, "vram_free_mib": 31000}, time.time())
check("a heartbeat means the agent runs again -> awake (via heartbeat)", n.power == "awake" and journal(n)[-1]["via"] == "heartbeat")

# 4) Untrusted input is dropped without touching the state
before = (n.power, n.state)
tun.dispatch(power_frame({"state": "hibernating"}))
tun.dispatch(power_frame(b"not json"))
tun.dispatch(power_frame(["suspending"]))
check("unknown state, broken JSON and a non-object are dropped", (n.power, n.state) == before, f"{n.power} {n.state}")

# 5) A pending (not approved) tunnel cannot change a node
n2, tun2 = add_node()
tun2.approved = False
tun2.dispatch(power_frame({"state": "suspending"}))
check("unapproved tunnel: no effect", n2.power is None and n2.state == "free")

# 6) Home Assistant is optional: the view carries the fact, the entity exists only for a publisher
n3, tun3 = add_node()
tun3.dispatch(power_frame({"state": "suspending", "via": "PBT_APMSUSPEND"}))
view = ha.ha_snapshot()["nodes"]["gpu-desktop"]
check("HA view (without MQTT configured) carries power and power_since", view["power"] == "asleep" and view["power_since"])
pub = ha.HAPublisher.__new__(ha.HAPublisher)
pub.base = "skirnir"
ents = {oid: (comp, label, extra) for comp, oid, label, extra in pub._node_entities("gpu-desktop")}
comp, label, extra = ents.get("node_gpu_desktop_asleep", (None, None, {}))
check("HA entity binary_sensor 'schläft' for each node, only through the (optional) publisher",
      comp == "binary_sensor" and label == "gpu-desktop schläft" and "power" in extra.get("value_template", ""), str(comp))
check("... unavailable while the agent never reported (no false 'awake')",
      any("power is not none" in a.get("value_template", "") for a in extra.get("availability", [])))

print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
