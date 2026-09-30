#!/usr/bin/env python3
"""Selbsttest (Router 0.4.1 / Agent 0.14.x): Ollama-Zustand vom Agenten (Rahmen OLLAMA, poll.apply_ollama_push).

Nachgestellt ist der Vorfall vom 2026-09-30 nach dem Update auf Agent 0.14.0: Ollama war beim Start des Agenten noch nicht
bereit (Meldung up=false, Knoten offline), danach antwortete es ohne geladenes Modell, und der Agent schickte `ps: null`. Der
Router verwarf das und behielt die letzte gueltige Meldung "offline", bis sie verfiel. Laeuft ohne Router, Netz und Ollama."""
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
state.CFG.prewarm_on_online = False   # kein Vorladen anstossen (kein Event-Loop, kein Knoten dahinter)
poll.evaluate = lambda node, now: None   # der Zustandsautomat ist hier nicht Gegenstand
state.CAPS["qwen3.8:27b"] = ["completion", "tools"]   # Faehigkeiten bekannt: kein Nachladen (braeuchte Event-Loop und Knoten)
QWEN_TAG = {"name": "qwen3.8:27b", "digest": "aa", "size": 17 * 2**30}
QWEN_PS = {"name": "qwen3.8:27b", "digest": "aa", "size_vram": 20 * 2**30, "context_length": 131072, "expires_at": "2026-09-30T22:00:00Z"}

n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
now = time.time()
check("vor der ersten Meldung: Router pollt", not n.ollama_push_active(now))

poll.apply_ollama_push(n, {"up": False, "error": "connection refused", "rev": 1}, now)
check("up=false (Ollama startet noch): Knoten offline, Meldung zaehlt, Router pollt nicht", n.state == "offline" and n.ollama_push_active(now)
      and "connection refused" in (n.ollama_push_error or ""))

poll.apply_ollama_push(n, {"up": True, "rev": 2, "tags": [QWEN_TAG], "ps": None}, now + 1)
check("Agent 0.14.0: ps=null gilt als leere Liste -> Knoten wieder free, Modelle bekannt, nichts geladen",
      n.state == "free" and n.models == {"qwen3.8:27b"} and n.loaded == {} and n.ollama_push_active(now + 1), f"{n.state} {n.models} {n.loaded}")

poll.apply_ollama_push(n, {"up": True, "rev": 3, "tags": [QWEN_TAG], "ps": [QWEN_PS]}, now + 2)
check("Modell geladen: Groesse und Kontextlaenge uebernommen (Kontext-Mitnutzung aus 0.3.7 braucht loaded_ctx)",
      abs(n.loaded.get("qwen3.8:27b", 0) - 20) < 0.01 and n.loaded_ctx.get("aa") == 131072, f"{n.loaded} {n.loaded_ctx}")

poll.apply_ollama_push(n, {"up": True, "rev": 4, "tags": "kaputt", "ps": []}, now + 3)
check("unbrauchbare Meldung: verworfen, Zustand bleibt, Router pollt ab sofort wieder (nicht die alte Meldung behalten)",
      not n.ollama_push_active(now + 3) and n.state == "free" and "qwen3.8:27b" in n.loaded, f"push_ts={n.ollama_push_ts}")

poll.apply_ollama_push(n, {"rev": 5}, now + 4)
check("Meldung ohne up: verworfen, Router pollt", not n.ollama_push_active(now + 4))

poll.apply_ollama_push(n, {"up": True, "rev": 6, "tags": [QWEN_TAG, {"kein": "name"}, 7], "ps": []}, now + 5)
check("Eintraege ohne Namen fallen weg, Meldung gilt", n.models == {"qwen3.8:27b"} and n.loaded == {} and n.ollama_push_active(now + 5))
check("frisch bis 45 s, danach wieder Poll", n.ollama_push_active(now + 5 + 44) and not n.ollama_push_active(now + 5 + 46))

# --- Tausch erst nach Freigabe (Router 0.5.0 / Agent 0.15.0): leeren, auf laufende Anfragen warten, dann freigeben
import asyncio  # noqa: E402

from skirnir_router import ollamaupdate, registry  # noqa: E402

GESENDET = []


async def send_ctl(tunnel, msg):
    GESENDET.append(msg)


class Reg:
    def __init__(self, e):
        self.nodes = {"fp1": e}

    def save(self):
        pass


registry.send_ctl = send_ctl
ollamaupdate.SWAP_WAIT_S = 1.5


async def tausch_szenario(inflight_endet):
    GESENDET.clear()
    k = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
    k.fp, k.tunnel, k.inflight, k.state = "fp1", object(), 1, "free"
    e = {"name": "gpu-desktop", "facts": {"ollama_swap_gate": True}, "ollama_update": {"version": "0.35.0", "state": "ready", "t": time.time()}}
    state.REG = Reg(e)
    t = asyncio.create_task(ollamaupdate.release_swap(k, "0.35.0"))
    await asyncio.sleep(0.6)
    vorher = (list(GESENDET), time.time() < k.draining_until)
    if inflight_endet:
        k.inflight = 0
    await t
    return vorher, list(GESENDET), e["ollama_update"], k


(vorher, nachher, u, k) = asyncio.run(tausch_szenario(True))
check("laufende Anfrage: Knoten aus dem Routing, aber noch keine Freigabe", vorher == ([], True), str(vorher))
check("Anfrage beendet: Tausch freigegeben", nachher == [{"t": "ollama-swap", "version": "0.35.0"}] and k.swap_pending is None, str(nachher))
(vorher, nachher, u, k) = asyncio.run(tausch_szenario(False))
check("Knoten wird nicht leer: keine Freigabe, Auftrag failed, Knoten wieder im Routing",
      nachher == [] and u["state"] == "failed" and "nicht leer" in u["message"] and time.time() >= k.draining_until, f"{nachher} {u}")
state.REG = Reg({"name": "gpu-desktop", "facts": {}, "ollama_update": {"version": "0.35.0", "state": "downloading", "t": time.time()}})
k = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
k.fp = "fp1"
check("waehrend eines Ollama-Updates kein Vorwaermen (update_running)", ollamaupdate.update_running(k))
state.REG.nodes["fp1"]["ollama_update"]["state"] = "done"
check("nach dem Update wieder Vorwaermen erlaubt", not ollamaupdate.update_running(k))
check("Agent ohne Freigabe-Schritt (< 0.15.0) erkannt", not ollamaupdate.swap_gate({"facts": {}}) and ollamaupdate.swap_gate({"facts": {"ollama_swap_gate": True}}))

# Update erledigt (Agent meldet die Zielversion): einmal vorwaermen, weil das Vorwaermen beim Neustart waehrend des Updates entfiel
GEWAERMT = []


async def prewarm_attrappe(node, delay, reason, since=None):
    GEWAERMT.append(reason)


async def fertig_melden(zustand):
    GEWAERMT.clear()
    k = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8})
    k.fp, k.state = "fp1", zustand
    state.REG = Reg({"name": "gpu-desktop", "facts": {"ollama_swap_gate": True},
                     "ollama_update": {"version": "0.35.0", "state": "applied", "t": time.time()}})
    ollamaupdate.note_version(k, "0.35.0", time.time(), source="agent")
    await asyncio.sleep(0.05)
    return state.REG.nodes["fp1"]["ollama_update"]["state"], list(GEWAERMT)


poll.prewarm = prewarm_attrappe
state.CFG.prewarm_on_online = True   # oben fuer die Push-Pruefungen abgeschaltet
r = asyncio.run(fertig_melden("free"))
check("Update erledigt, Knoten frei: einmal vorwaermen", r == ("done", ["nach Ollama-Update"]), str(r))
check("Update erledigt, Knoten belegt (Spiel): nicht vorwaermen", asyncio.run(fertig_melden("busy")) == ("done", []))

print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
