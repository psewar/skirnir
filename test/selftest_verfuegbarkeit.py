#!/usr/bin/env python3
"""Selbsttest der Grund-Codes fuer "kein Knoten" (scheduler.blockers / scheduler.unavailable). Laeuft ohne Router und
ohne Netz, mit Attrappen fuer Knoten und Konfiguration.

Anlass 2026-09-29: ein Spiel belegte auf gpu-desktop 5 GiB VRAM, qwen3.6@131k passte nicht mehr ins Budget, der Knoten
stand aber auf "free". Der Router antwortete nur "no node available"; der Agent-Client konnte weder warten noch sagen, woran es
lag. Wichtig ist hier die Abgrenzung: gpu_busy nur, wenn das fremde VRAM (oder der busy-Zustand) wirklich der Grund
ist - ein Modell, das auch auf die leere Karte nicht passt, ist model_too_large, und darauf zu warten waere sinnlos.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import scheduler as S   # noqa: E402
from skirnir_router import state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class Cfg:
    reserve = {"free": 1.0, "busy": 2.0}
    need = {"qwen3.6:35b-a3b": 24.0, "riesig:1t": 40.0, "klein:8b": 6.0}

    def need_gib(self, model, ctx, node=None):
        return self.need[model]


class Knoten:
    """Nur was _node_block liest. budget_gib wie Node.budget_gib: freies VRAM + Ollamas eigenes - Reserve."""

    def __init__(self, name, models, st="free", free=30.0, total=31.8, foreign=0.0, loaded=(), breaker="closed",
                 draining_until=0.0, busy_reason="", gpu_util=0, game=None, memory=None, mem_pressure=False):
        self.name, self.models, self.state = name, set(models), st
        self.vram_free_gib, self.vram_total_gib, self._foreign = free, total, foreign
        self.loaded = {m: 20.0 for m in loaded}
        self.breaker, self.draining_until, self.busy_reason, self.gpu_util = breaker, draining_until, busy_reason, gpu_util
        self.game, self.memory, self.mem_pressure = game, memory, mem_pressure

    def foreign_vram_gib(self):
        return self._foreign

    def breaker_would_allow(self, now):
        return self.breaker == "closed"

    def is_loaded(self, model):
        return model in self.loaded

    def loaded_context(self, model):
        return None

    def budget_gib(self, now, model):
        return self.vram_free_gib + sum(self.loaded.values()) - state.CFG.reserve.get(self.state, 1.0)


def knoten(*ns):
    state.NODES.clear()
    for n in ns:
        state.NODES[n.name] = n


state.CFG = Cfg()
NACHT = [{"model": "qwen3.6:35b-a3b", "num_ctx": 131072, "busy_ok": False}]

# --- der Fall vom 2026-09-29: free, aber ein Spiel belegt 5 GiB -> Budget 19.4 < 24 ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=20.4, foreign=5.0, gpu_util=35))
u = S.unavailable(NACHT, None, 0)
b = u["blockers"][0] if u["blockers"] else {}
check("free + fremdes VRAM nimmt den Platz -> gpu_busy", u["code"] == S.GPU_BUSY, str(u))
check("... mit Zahlen fuer den Client", b.get("need_gib") == 24.0 and b.get("budget_gib") == 19.4 and b.get("foreign_vram_gib") == 5.0, str(b))
check("... Grund foreign_vram, Retry-After gesetzt", b.get("busy_reason") == "foreign_vram" and u["retry_after_s"] == S.RETRY_AFTER_S[S.GPU_BUSY], str(u))
check("... Klartext nennt Knoten, fremde GiB und Bedarf", all(s in u["detail"] for s in ("gpu-desktop", "5.0 GiB", "24.0 GiB")), u["detail"])

# --- Gegenprobe: ohne fremdes VRAM fehlt der Platz aus anderem Grund -> nicht gpu_busy ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=20.4, foreign=0.0))
check("Platz fehlt ohne fremdes VRAM -> vram_full, nicht gpu_busy", S.unavailable(NACHT, None, 0)["code"] == S.VRAM_FULL)

# --- fremdes VRAM da, haette aber auch ohne es nicht gereicht -> vram_full ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=10.0, foreign=2.0))
check("fremdes VRAM zu klein, um die Luecke zu erklaeren -> vram_full", S.unavailable(NACHT, None, 0)["code"] == S.VRAM_FULL)

# --- Modell passt auch auf die leere Karte nicht -> model_too_large, kein Retry-After ---
riesig = [{"model": "riesig:1t", "num_ctx": 8192, "busy_ok": False}]
knoten(Knoten("gpu-desktop", ["riesig:1t"], free=20.0, foreign=5.0))
u = S.unavailable(riesig, None, 0)
check("passt nie -> model_too_large ohne Retry-After", u["code"] == S.MODEL_TOO_LARGE and u["retry_after_s"] is None, str(u))

# --- Knoten busy (Spiel mit GPU-Last) -> gpu_busy mit busy_reason ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], st="busy", free=25.0, foreign=4.3, busy_reason="gpu_util", gpu_util=65))
u = S.unavailable(NACHT, None, 0)
check("busy durch gpu_util -> gpu_busy", u["code"] == S.GPU_BUSY and u["blockers"][0]["busy_reason"] == "gpu_util", str(u))

# --- Agent >= 0.17.0: Spiel als Fakt -> gpu_busy nennt das Spiel ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], st="busy", busy_reason="game", game={"name": "Diablo IV.exe"}))
u = S.unavailable(NACHT, None, 0)
check("busy durch Spiel -> gpu_busy mit Spielname", u["code"] == S.GPU_BUSY and u["blockers"][0].get("game") == "Diablo IV.exe"
      and "Diablo IV.exe" in u["detail"], str(u))

# --- Speicher knapp: kalt laden gesperrt, warm geladen bleibt bedienbar ---
knapp = {"ram_available_gib": 2.5, "commit_free_gib": 1.8, "ram_total_gib": 64.0}
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=30.0, memory=knapp, mem_pressure=True))
u = S.unavailable(NACHT, None, 0)
check("Speicher knapp + kalt -> memory_pressure mit Zahlen", u["code"] == S.MEMORY_PRESSURE
      and u["blockers"][0].get("commit_free_gib") == 1.8 and "ram_total_gib" not in u["blockers"][0], str(u))
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=30.0, memory=knapp, mem_pressure=True, loaded=["qwen3.6:35b-a3b"]))
check("Speicher knapp + warm geladen -> kein Hindernis", S.blockers(NACHT, None, 0) == [])

# --- busy_ok-Stufe laeuft trotz busy: kein Hindernis ---
knoten(Knoten("gpu-desktop", ["klein:8b"], st="busy", free=25.0))
check("busy_ok-Stufe mit Platz -> keine Hindernisse", S.blockers([{"model": "klein:8b", "num_ctx": 8192, "busy_ok": True}], None, 0) == [])

# --- warm geladen: kein Budget noetig, kein Hindernis ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=1.0, foreign=5.0, loaded=["qwen3.6:35b-a3b"]))
check("warm geladenes Modell ist kein Hindernis, auch bei vollem VRAM", S.blockers(NACHT, None, 0) == [])

# --- zwei Knoten: einer offline, einer busy -> gpu_busy gewinnt (vergeht von selbst) ---
knoten(Knoten("mary", ["qwen3.6:35b-a3b"], st="offline"),
       Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], st="busy", busy_reason="foreign_vram"))
u = S.unavailable(NACHT, None, 0)
check("offline + busy -> gpu_busy, beide als Hindernis gelistet", u["code"] == S.GPU_BUSY and len(u["blockers"]) == 2, str(u))

# --- nur offline ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], st="offline"))
check("nur offline -> node_offline", S.unavailable(NACHT, None, 0)["code"] == S.NODE_OFFLINE)

# --- Agent-Update / Breaker ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], draining_until=10.0))
check("Agent-Update laeuft -> node_draining", S.unavailable(NACHT, None, 0)["code"] == S.NODE_DRAINING)
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], breaker="open"))
check("Breaker offen -> node_failing", S.unavailable(NACHT, None, 0)["code"] == S.NODE_FAILING)

# --- kein Knoten hat das Modell ---
knoten(Knoten("gpu-desktop", ["klein:8b"]))
check("Modell nirgends vorhanden -> model_missing", S.unavailable(NACHT, None, 0)["code"] == S.MODEL_MISSING)

# --- Kandidat war da, Versuch scheiterte (tried) -> attempt_failed ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=30.0))
u = S.unavailable(NACHT, None, 0, exclude={"gpu-desktop"})
check("bedienbarer Knoten in tried -> attempt_failed", u["code"] == S.ATTEMPT_FAILED, str(u))

# --- Client-Kontext kleiner als die Stufe wird uebernommen ---
knoten(Knoten("gpu-desktop", ["qwen3.6:35b-a3b"], free=20.4, foreign=5.0))
check("num_ctx des Clients steht im Hindernis", S.blockers(NACHT, 32768, 0)[0]["num_ctx"] == 32768)

print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
sys.exit(1 if FAILS else 0)
