#!/usr/bin/env python3
"""Self-test (Router 0.8.8): Retry prewarming after a light game; reset busy_reason on coming online.

Trigger 2026-10-21: After an agent update, a node came online with a running (light) game. Prewarming fell into the
game_sustain window (game not yet evaluated) and was skipped; no one re-planned it later, leaving the card idle next
to the light game. Additionally, /admin/state showed "free" with the stale busy reason "game" from before the outage.
Runs without Router, network, or Ollama (real config from router/config.example.yaml); state.spawn is written to."""
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
    """state.spawn stub: records which prewarming would be planned with which reason, and closes the coroutine."""
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


# 1) Coming online resets the stale busy reason
n = knoten()
n.state, n.busy_reason, n.calm_since = "offline", "game", time.time() - 5
GEPLANT.clear()
poll.apply_ollama_state(n, [], [], time.time())
check("offline -> online: free und busy_reason leer (nicht mehr 'game' von vor dem Ausfall)", n.state == "free" and n.busy_reason == "",
      f"{n.state} {n.busy_reason!r}")
check("... und das Online-Vorwaermen ist geplant", vorwaermen_geplant() == ["online"], str(GEPLANT))

# 2) Prewarming falls into the game_sustain window: skipped, but noted
n = knoten(LEICHT, seit=1)
asyncio.run(poll.prewarm(n, 0, "online", since=n.free_at))
check("Spiel noch nicht beurteilt (1 s alt): Vorwaermen uebersprungen und vorgemerkt", n.prewarm_after_game is True,
      str(n.prewarm_after_game))
GEPLANT.clear()
poll.evaluate(n, time.time())
check("... im Fenster wird noch nicht nachgeholt", vorwaermen_geplant() == [] and n.prewarm_after_game is True, str(GEPLANT))

# 3) Window passed, game is light -> retry (once)
n.game_since = time.time() - SUSTAIN - 1
GEPLANT.clear()
poll.evaluate(n, time.time())
check("Spiel als leicht beurteilt -> Vorwaermen nachgeholt (Grund spiel-leicht), Vormerkung geloescht",
      vorwaermen_geplant() == ["game_light"] and n.prewarm_after_game is False and n.state == "free", f"{GEPLANT} {n.state}")
GEPLANT.clear()
poll.evaluate(n, time.time())
check("... nur einmal", vorwaermen_geplant() == [], str(GEPLANT))

# 4) Game requires GPU -> no retry; node becomes busy (busy -> free prewarms later on its own)
n = knoten(SCHWER, seit=SUSTAIN + 1)
n.prewarm_after_game = True
GEPLANT.clear()
poll.evaluate(n, time.time())
check("schweres Spiel: busy, kein Nachholen, Vormerkung geloescht", n.state == "busy" and vorwaermen_geplant() == []
      and n.prewarm_after_game is False, f"{n.state} {GEPLANT} {n.prewarm_after_game}")

# 5) Game ended before evaluation -> retry (reason: game-ended)
n = knoten()
n.prewarm_after_game = True
GEPLANT.clear()
poll.evaluate(n, time.time())
check("Spiel beendet, Knoten frei -> Vorwaermen nachgeholt (spiel-vorbei)", vorwaermen_geplant() == ["game_ended"], str(GEPLANT))

# 6) Memory pressure holds back the retry (prewarming would pause there anyway). Without a running game: a
#    light game under memory pressure is treated as heavy since 0.8.7 and makes the node busy - the E2E run checks this.
n = knoten()
n.prewarm_after_game, n.mem_pressure = True, True
GEPLANT.clear()
poll.evaluate(n, time.time())
check("Speicherdruck: nicht nachholen, Vormerkung bleibt", vorwaermen_geplant() == [] and n.prewarm_after_game is True,
      f"{GEPLANT} {n.prewarm_after_game} {n.state} {n.game_heavy_why}")

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
