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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, poll, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
PLANNED = []


def record(coro):
    """state.spawn stub: records which prewarming would be planned with which reason, and closes the coroutine."""
    f = getattr(coro, "cr_frame", None)
    PLANNED.append((getattr(coro, "__name__", "?"), (f.f_locals.get("reason") if f else None)))
    coro.close()


state.spawn = record
SUSTAIN = state.CFG.game_sustain
LIGHT = {"running": True, "name": "Small Game.exe", "via": "pfad", "procs": 1, "gpu_dedicated_mib": 735, "gpu_shared_mib": 32, "gpu_pct": 1.8}
HEAVY = dict(LIGHT, gpu_dedicated_mib=300, gpu_shared_mib=2100)


def add_node(game=None, since=None):
    now = time.time()
    n = nodes.Node("gpu-desktop", {"vram_total_gib": 31.8, "foreign_vram_baseline_gib": 2.0})
    n.state, n.hb_ts, n.free_at = "free", now, now
    n.gpu_util, n.vram_total_gib, n.vram_used_gib, n.vram_free_gib = 2, 31.8, 3.0, 28.8
    if game:
        n.game, n.game_since = dict(game), now - since
    return n


def prewarm_planned():
    return [r for name, r in PLANNED if name == "prewarm"]


# 1) Coming online resets the stale busy reason
n = add_node()
n.state, n.busy_reason, n.calm_since = "offline", "game", time.time() - 5
PLANNED.clear()
poll.apply_ollama_state(n, [], [], time.time())
check("offline -> online: free and busy_reason empty (no longer 'game' from before the failure)", n.state == "free" and n.busy_reason == "",
      f"{n.state} {n.busy_reason!r}")
check("... and online prewarming is scheduled", prewarm_planned() == ["online"], str(PLANNED))

# 2) Prewarming falls into the game_sustain window: skipped, but noted
n = add_node(LIGHT, since=1)
asyncio.run(poll.prewarm(n, 0, "online", since=n.free_at))
check("Game not yet evaluated (1 s old): prewarming skipped and noted", n.prewarm_after_game is True,
      str(n.prewarm_after_game))
PLANNED.clear()
poll.evaluate(n, time.time())
check("... not yet caught up within the window", prewarm_planned() == [] and n.prewarm_after_game is True, str(PLANNED))

# 3) Window passed, game is light -> retry (once)
n.game_since = time.time() - SUSTAIN - 1
PLANNED.clear()
poll.evaluate(n, time.time())
check("Game evaluated as light -> prewarming caught up (reason game-light), note deleted",
      prewarm_planned() == ["game_light"] and n.prewarm_after_game is False and n.state == "free", f"{PLANNED} {n.state}")
PLANNED.clear()
poll.evaluate(n, time.time())
check("... only once", prewarm_planned() == [], str(PLANNED))

# 4) Game requires GPU -> no retry; node becomes busy (busy -> free prewarms later on its own)
n = add_node(HEAVY, since=SUSTAIN + 1)
n.prewarm_after_game = True
PLANNED.clear()
poll.evaluate(n, time.time())
check("heavy game: busy, no catch-up, note deleted", n.state == "busy" and prewarm_planned() == []
      and n.prewarm_after_game is False, f"{n.state} {PLANNED} {n.prewarm_after_game}")

# 5) Game ended before evaluation -> retry (reason: game-ended)
n = add_node()
n.prewarm_after_game = True
PLANNED.clear()
poll.evaluate(n, time.time())
check("Game ended, node free -> prewarming caught up (game-over)", prewarm_planned() == ["game_ended"], str(PLANNED))

# 6) Memory pressure holds back the retry (prewarming would pause there anyway). Without a running game: a
#    light game under memory pressure is treated as heavy since 0.8.7 and makes the node busy - the E2E run checks this.
n = add_node()
n.prewarm_after_game, n.mem_pressure = True, True
PLANNED.clear()
poll.evaluate(n, time.time())
check("Memory pressure: no catch-up, note remains", prewarm_planned() == [] and n.prewarm_after_game is True,
      f"{PLANNED} {n.prewarm_after_game} {n.state} {n.game_heavy_why}")

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
