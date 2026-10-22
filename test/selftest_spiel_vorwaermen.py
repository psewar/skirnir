#!/usr/bin/env python3
"""Selbsttest (Router 0.8.8): Vorwaermen nach einem leichten Spiel nachholen; busy_reason beim Online-Gehen zuruecksetzen.

Anlass 2026-10-21: nach einem Agent-Update kam ein Knoten mit laufendem (leichtem) Spiel online. Das Vorwaermen fiel ins
game_sustain-Fenster (Spiel noch nicht beurteilt) und wurde uebersprungen; spaeter plante es niemand neu, die Karte blieb neben
dem leichten Spiel leer. Ausserdem zeigte /admin/state "free" mit dem busy-Grund "game" von vor dem Ausfall.
Laeuft ohne Router, Netz und Ollama (echte Config aus router/config.example.yaml); state.spawn wird mitgeschrieben."""
import asyncio
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
GEPLANT = []


def mitschreiben(coro):
    """state.spawn-Ersatz: merkt sich, welches Vorwaermen mit welchem Grund geplant wuerde, und schliesst die Coroutine."""
    f = getattr(coro, "cr_frame", None)
    GEPLANT.append((getattr(coro, "__name__", "?"), (f.f_locals.get("reason") if f else None)))
    coro.close()


state.spawn = mitschreiben
SUSTAIN = state.CFG.game_sustain
LEICHT = {"running": True, "name": "Small Game.exe", "via": "pfad", "procs": 1, "gpu_dedicated_mib": 735, "gpu_shared_mib": 32, "gpu_pct": 1.8}
SCHWER = dict(LEICHT, gpu_dedicated_mib=300, gpu_shared_mib=2100)


def knoten(spiel=None, seit=None):
    now = time.time()
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "foreign_vram_baseline_gib": 2.0})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 2, 31.8, 3.0, 28.8
    if spiel:
        n.game, n.game_since = dict(spiel), now - seit
    return n


def vorwaermen_geplant():
    return [r for name, r in GEPLANT if name == "prewarm"]


# 1) Online-Gehen setzt den alten busy-Grund zurueck
n = knoten()
n.state, n.busy_reason, n.calm_since = "offline", "game", time.time() - 5
GEPLANT.clear()
poll.apply_ollama_state(n, [], [], time.time())
check("offline -> online: free und busy_reason leer (nicht mehr 'game' von vor dem Ausfall)", n.state == "free" and n.busy_reason == "",
      f"{n.state} {n.busy_reason!r}")
check("... und das Online-Vorwaermen ist geplant", vorwaermen_geplant() == ["online"], str(GEPLANT))

# 2) Vorwaermen faellt ins game_sustain-Fenster: uebersprungen, aber vorgemerkt
n = knoten(LEICHT, seit=1)
asyncio.run(poll.prewarm(n, 0, "online", since=n.free_at))
check("Spiel noch nicht beurteilt (1 s alt): Vorwaermen uebersprungen und vorgemerkt", n.prewarm_after_game is True,
      str(n.prewarm_after_game))
GEPLANT.clear()
poll.evaluate(n, time.time())
check("... im Fenster wird noch nicht nachgeholt", vorwaermen_geplant() == [] and n.prewarm_after_game is True, str(GEPLANT))

# 3) Fenster vorbei, Spiel leicht -> nachholen (einmal)
n.game_since = time.time() - SUSTAIN - 1
GEPLANT.clear()
poll.evaluate(n, time.time())
check("Spiel als leicht beurteilt -> Vorwaermen nachgeholt (Grund spiel-leicht), Vormerkung geloescht",
      vorwaermen_geplant() == ["game_light"] and n.prewarm_after_game is False and n.state == "free", f"{GEPLANT} {n.state}")
GEPLANT.clear()
poll.evaluate(n, time.time())
check("... nur einmal", vorwaermen_geplant() == [], str(GEPLANT))

# 4) Spiel braucht die GPU -> kein Nachholen; der Knoten wird busy (busy -> free waermt spaeter selbst vor)
n = knoten(SCHWER, seit=SUSTAIN + 1)
n.prewarm_after_game = True
GEPLANT.clear()
poll.evaluate(n, time.time())
check("schweres Spiel: busy, kein Nachholen, Vormerkung geloescht", n.state == "busy" and vorwaermen_geplant() == []
      and n.prewarm_after_game is False, f"{n.state} {GEPLANT} {n.prewarm_after_game}")

# 5) Spiel vorbei, bevor es beurteilt war -> nachholen (Grund spiel-vorbei)
n = knoten()
n.prewarm_after_game = True
GEPLANT.clear()
poll.evaluate(n, time.time())
check("Spiel beendet, Knoten frei -> Vorwaermen nachgeholt (spiel-vorbei)", vorwaermen_geplant() == ["game_ended"], str(GEPLANT))

# 6) Speicherdruck haelt das Nachholen zurueck (das Vorwaermen wuerde dort ohnehin aussetzen). Ohne laufendes Spiel: ein
#    leichtes Spiel bei Speicherdruck gilt seit 0.8.7 als schwer und macht busy - das prueft der E2E-Lauf.
n = knoten()
n.prewarm_after_game, n.mem_pressure = True, True
GEPLANT.clear()
poll.evaluate(n, time.time())
check("Speicherdruck: nicht nachholen, Vormerkung bleibt", vorwaermen_geplant() == [] and n.prewarm_after_game is True,
      f"{GEPLANT} {n.prewarm_after_game} {n.state} {n.game_heavy_why}")

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
