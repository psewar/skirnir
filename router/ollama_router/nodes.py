"""Ein GPU-Knoten: Zustand, VRAM-Rechnung, Baseline, und `nreq` als einziger Weg zu seinem Ollama."""

import asyncio
import time
from collections import Counter

from aiohttp import ClientError, Fingerprint

from . import config, state
from .common import log


class NodeNotReady(ClientError):
    """TLS-Knoten, dessen Fingerprint noch nicht bekannt ist (kein Heartbeat bisher)."""


def nreq(node, method, path, **kw):
    """HTTP-Anfrage an einen Knoten: durch den Tunnel des Agenten, wenn er steht; sonst direkt an die ollama-URL
    (TLS-Knoten: gepinntes Zertifikat + Router-Token). Knoten ohne URL und ohne Tunnel gelten als nicht erreichbar."""
    if node.tunnel is not None:
        return node.tunnel.request(method, path, **kw)
    if not node.url:
        raise NodeNotReady(f"{node.name}: kein Tunnel verbunden")
    if node.tls:
        if node.ssl is None:
            raise NodeNotReady(f"{node.name}: TLS-Fingerprint unbekannt, warte auf Heartbeat")
        kw["ssl"] = node.ssl
        h = dict(kw.get("headers") or {})
        if state.CFG.heartbeat_token:
            h["X-Router-Token"] = state.CFG.heartbeat_token
        kw["headers"] = h
    return state.SESSION.request(method, node.url + path, **kw)


class Node:
    def set_fingerprint(self, hexfp):
        fp = hexfp.lower().removeprefix("sha256:").replace(":", "").strip()
        if len(fp) != 64:
            raise ValueError(f"TLS-Fingerprint ungueltig: {hexfp!r}")
        self.tls_fp = fp
        self.ssl = Fingerprint(bytes.fromhex(fp))

    def __init__(self, name, spec):
        self.name = name
        self.url = (spec.get("ollama") or "").rstrip("/")   # leer = Knoten kommt nur per Tunnel (Agent verbindet sich)
        self.tunnel = None
        # TLS-Knoten (Agent-Vorschaltstelle vor Ollama): Zertifikat wird per SHA-256-Fingerprint gepinnt, keine CA.
        # "auto": der Agent meldet den Fingerprint seines Zertifikats im Heartbeat (der ist per Token + LE-TLS gesichert).
        self.tls = self.url.startswith("https://")
        self.ssl = None
        self.tls_fp = ""
        self.tls_mode = "auto"
        fp = spec.get("ollama_tls_fingerprint")
        if spec.get("ollama_tls_cert"):
            fp = config.cert_fingerprint(spec["ollama_tls_cert"])
        if fp:
            self.tls_mode = "fixed"
            self.set_fingerprint(fp)
        self.vram_total_gib = float(spec.get("vram_total_gib", 0))
        self.mac = spec.get("mac")
        self.wol = bool(spec.get("wol", False))
        self.weight = int(spec.get("weight", 1))
        self.foreign_baseline_gib = float(spec.get("foreign_vram_baseline_gib", 0))
        self.fp = None      # Fingerprint des Agenten-Schluessels (registrierte Knoten)
        self.polled_ok = 0.0   # letzter erfolgreicher Poll (/api/ps bekannt) - Voraussetzung fuers Baseline-Lernen
        self.reg = None     # Eintrag im Knotenregister (nodes.json)
        self.gpu = spec.get("gpu", "")
        # Ollama-Sicht
        self.models = set()
        self.last_known_models = set()
        self.model_details = {}          # name -> dict aus /api/tags
        self.digest_of = {}              # name -> digest (Alias und Original teilen den Digest)
        self.loaded = {}                 # geladener Name -> size_vram GiB (so wie Ollama ihn meldet)
        self.loaded_digest = {}          # digest -> size_vram GiB
        self.loaded_ctx = {}             # digest -> context_length, mit dem das Modell geladen ist
        self.misses = 0
        self.state = "offline"
        # Agent-Sicht
        self.hb_ts = 0.0
        self.gpu_util = None
        self.sensors = None   # Zusatzsensoren aus dem Heartbeat (Agent >= 0.6.0), dict oder None
        self.guard = None     # GPU-Schutz laut Agent (>= 0.7.0): state, limit_w, target_w, problem, grund, warnungen
        self.draining_until = 0.0   # Agent-Update laeuft: bis dahin keine neuen Anfragen an diesen Knoten
        self.guard_ts = 0.0
        self.vram_free_gib = None
        self.vram_used_gib = None
        self.vram_total_reported_gib = None
        self.ollama_proc_gib = None
        self.vram_last_gib = 0.0      # Ollama-Belegung beim letzten Poll, um ein Schrumpfen zu erkennen
        self.vram_hold_gib = 0.0      # solange nvidia-smi nachzieht, gilt dieser Wert als Ollama-Belegung
        self.vram_hold_until = 0.0
        self.loading = {}             # Modell -> (erwartete Belegung GiB, Frist): Ladevorgang, den der Router ausgeloest hat
        # Zustandsautomat
        self.hot_since = None
        self.foreign_since = None
        self.last_busy_unload = 0.0
        self.calm_since = None
        self.busy_reason = ""
        # Laufende Arbeit
        self.inflight = 0
        self.inflight_models = Counter()
        self.last_used = {}           # Modell -> Zeitpunkt der letzten Anfrage ueber den Router (Residenz-Regel)
        self.last_residency = 0.0
        self.free_since = 0.0         # seit wann ununterbrochen free (gefuehrt von poll.residency_check)
        # Stufe 3: Circuit Breaker (closed -> open nach `failures` Backend-Fehlern im Fenster -> half_open nach open_s:
        # eine Probe, Erfolg = closed, Fehler = wieder open) und Admission (max_inflight aus der Policy, sonst Default)
        self.breaker = "closed"
        self.breaker_fails = []
        self.breaker_until = 0.0
        self.max_inflight = int(spec["max_inflight"]) if spec.get("max_inflight") else None
        # Busy-Schwellen je Knoten (Policy im Register, 2026-09-11): eine 16-GiB-Karte, auf der Ollama 8 GiB haelt, laesst
        # einem Spiel nie 6 GiB "fremdes" VRAM - gpu-laptop blieb beim Spielen free (util 35-40 %, fremd 3,9 GiB) und gemma4 blieb
        # geladen. None = globale Werte aus modes.busy_enter.
        self.busy_util_pct = float(spec["busy_gpu_util_pct"]) if spec.get("busy_gpu_util_pct") is not None else None
        self.busy_foreign_pol = float(spec["busy_foreign_gib"]) if spec.get("busy_foreign_gib") is not None else None
        self.guard_policy = bool(spec.get("gpu_guard", True))   # Policy je Knoten: Router beachtet den GPU-Schutz-Status (Deckel, Score, HA)
        self.foreign_frozen = 0.0     # fremdes VRAM beim Start eines Ladevorgangs (siehe announce_load / foreign_vram_gib)
        self.ollama_version = None    # Stufe 6: Supply Chain (aus /api/version, alle 5 min)
        self.version_ts = 0.0
        # WOL
        self.last_wake = 0.0
        self.wake_lock = asyncio.Lock()

    def gpu_known(self, now):
        return self.hb_ts and (now - self.hb_ts) <= state.CFG.hb_stale_s

    def ollama_vram_gib(self):
        """Was `/api/ps` gerade meldet. Fuer das VRAM-Budget: dort zaehlt, was wirklich belegt ist."""
        return sum(self.loaded.values())

    def ollama_vram_claimed_gib(self):
        """Was Ollama beansprucht, inklusive Nachlauf. `/api/ps` (Poll) und die belegte Gesamtmenge aus nvidia-smi
        (Heartbeat) hinken einander nach, in beide Richtungen: beim Entladen faellt /api/ps sofort auf null, waehrend
        nvidia-smi den Speicher noch zeigt; beim Laden sieht nvidia-smi das Modell, bevor es in /api/ps auftaucht.
        Beides erschiene als fremdes VRAM. Solange der Nachlauf laeuft, gilt darum der hoehere Wert; hat nvidia-smi
        nachgezogen, klemmt `foreign_vram_gib()` das ohnehin auf 0."""
        now = time.time()
        claim = self.load_claim_gib(now)
        if now < self.vram_hold_until:
            claim = max(claim, self.vram_hold_gib)
        return max(sum(self.loaded.values()), claim)

    def hold_vram(self, now, gib):
        """Nachlauf setzen: bis `vram_settle_s` gilt mindestens `gib` als Ollama-Belegung."""
        if gib > 0:
            self.vram_hold_gib = max(gib, self.vram_hold_gib if now < self.vram_hold_until else 0.0)
            self.vram_hold_until = now + state.CFG.vram_settle_s

    def announce_load(self, model, ctx):
        """Der Router loest gerade einen Ladevorgang aus: die erwartete Belegung gilt ab sofort als beansprucht.
        Ohne das zaehlt das ladende Modell als fremdes VRAM - und ein Kaltstart, der laenger dauert als
        `foreign_sustain_s`, wuerde den Knoten mitten im Laden busy machen; das Sicherheitsnetz entlaedt dann genau
        das eben geladene Modell. Der Anspruch gilt, BIS das Modell in /api/ps steht (nicht nur `vram_settle_s`,
        denn genau der langsame Kaltstart ist der gefaehrliche Fall); `finish_load` beendet ihn mit kurzem Nachlauf,
        die Frist faengt einen verlorenen Abschluss ab.

        Erwartet wird, was `/api/ps` nachher als `size_vram` meldet, also `need_gib` OHNE den Fit-Zuschlag - und
        NICHT die aktuelle Belegung plus die neue: Ollama verdraengt beim Laden, die Summe waere fast doppelt so
        hoch wie die Wirklichkeit und wuerde die busy-Erkennung fuer die Dauer des Anspruchs aushebeln. Haelt Ollama
        daneben noch ein anderes Modell, deckt `max(...)` mit der `/api/ps`-Summe das ab."""
        if not self.loading:
            self.foreign_frozen = self.foreign_vram_gib()   # Stand VOR dem Anspruch einfrieren
        self.loading[model] = (max(0.0, state.CFG.need_gib(model, ctx, self) - state.CFG.overhead_gib), time.time() + 300)

    def finish_load(self, model):
        """Ladevorgang beendet (auch bei Fehler): Anspruch in einen kurzen Nachlauf umwandeln, damit die Luecke
        bis zum naechsten /api/ps-Poll gedeckt bleibt."""
        claim = self.loading.pop(model, None)
        if claim:
            self.hold_vram(time.time(), claim[0])

    def load_claim_gib(self, now):
        return max((gib for gib, deadline in self.loading.values() if now < deadline), default=0.0)

    def note_loaded_changed(self, now):
        """Nach jedem Poll aufrufen: schrumpft die Ollama-Belegung, beginnt der Nachlauf."""
        cur = sum(self.loaded.values())
        if cur < self.vram_last_gib - 0.05:
            self.hold_vram(now, self.vram_last_gib)
        self.vram_last_gib = cur
        for m in [m for m in self.loading if self.is_loaded(m)]:
            self.finish_load(m)   # /api/ps bestaetigt es: Anspruch in einen kurzen Nachlauf umwandeln

    def is_loaded(self, model):
        """Geladen = gleicher Digest, egal unter welchem Namen (ein Alias und sein Basismodell)."""
        d = self.digest_of.get(model)
        return (d is not None and d in self.loaded_digest) or model in self.loaded

    def loaded_size(self, model):
        d = self.digest_of.get(model)
        if d is not None and d in self.loaded_digest:
            return self.loaded_digest[d]
        return self.loaded.get(model, 0.0)

    def loaded_context(self, model):
        d = self.digest_of.get(model)
        return self.loaded_ctx.get(d) if d is not None else None

    def same_blob(self, a, b):
        da, db = self.digest_of.get(a), self.digest_of.get(b)
        return a == b or (da is not None and da == db)

    def foreign_vram_gib(self):
        """VRAM, das weder Ollama noch der bekannte Desktop-Grundverbrauch belegt."""
        if self.vram_used_gib is None:
            return 0.0
        if self.ollama_proc_gib is not None:
            ollama = self.ollama_proc_gib
        else:
            ollama = self.ollama_vram_claimed_gib()
        raw = max(0.0, self.vram_used_gib - ollama - self.baseline_gib())
        # Waehrend eines vom Router ausgeloesten Ladevorgangs deckt der Anspruch (announce_load) das ganze Modell ab, auch
        # solange es noch gar nicht im VRAM liegt - ein Spiel verschwand so 300 s lang aus der Rechnung (gpu-laptop 2026-09-11:
        # fremd 0,0 trotz 2,4 GiB Spiel, Knoten blieb free). Darum gilt mindestens der Wert von vor dem Laden.
        if self.load_claim_gib(time.time()) > 0:
            return max(raw, self.foreign_frozen)
        return raw

    def baseline_gib(self):
        """Desktop-Grundverbrauch ohne Ollama: Policy-Wert (Config/UI) und gelernter Wert (rollendes 24-h-Minimum von
        belegt minus Ollama); ist beides da, gilt das Minimum - ein Spiel beim ersten Start kann das Lernen so nicht verderben."""
        b = self.foreign_baseline_gib
        if self.reg is not None:
            pol = self.reg.get("policy") or {}
            if pol.get("foreign_vram_baseline_gib") is not None:
                b = float(pol["foreign_vram_baseline_gib"])
            learned = (self.reg.get("learned") or {}).get("baseline_gib")
            if learned is not None:
                b = min(b, learned) if b else learned
        return b

    def learn_baseline(self, now, sample):
        """Desktop-Grundverbrauch lernen: pro Stunde Anzahl/Summe/Minimum von (VRAM belegt - Ollama) in
        `nodes.json`, daraus der **Median der Stundenmittel** ueber 24 h.

        Bis 2026-09-08 war es das Minimum ueber alles. Das rutscht auf den ruhigsten Moment des Tages - auf
        gpu-desktop 2,89 GiB bei schlafendem Monitor, waehrend der wache Desktop 6,1 GiB braucht. Die Differenz galt
        als fremdes VRAM und machte den Knoten grundlos busy (zweimal in vier Minuten, GPU-Auslastung 0). Ein
        Minimum beschreibt eben nicht den Normalbetrieb, sondern dessen Extrem.

        Zwei Schichten gegen Verunreinigung: gesammelt wird nur bei ruhiger GPU (Filter beim Aufrufer), und der
        Median ueber die Stunden faengt einzelne trotzdem verdorbene Stunden ab. Ausreisser nach oben (ein Spiel)
        koennen den Median nur verschieben, wenn sie die Mehrheit der Stunden stellen - und dann deckelt ihn noch
        der Policy-Wert in `baseline_gib()`."""
        if self.reg is None or sample is None:
            return
        learned = self.reg.setdefault("learned", {})
        buckets = learned.setdefault("buckets", {})
        hour = int(now // 3600)
        # Eimer im alten Format (nur eine Zahl = Stundenminimum) verwerfen statt umrechnen: sie entstanden OHNE
        # den Ruhe-Filter, es stecken also Spielstunden darin. Auf gpu-desktop waeren das 19 von 23 Stunden bei
        # 18,2 GiB gewesen - der Median daraus waere schlechter als alles, was wir ersetzen wollen.
        for k, v in [(k, v) for k, v in buckets.items() if not isinstance(v, dict)]:
            del buckets[k]
        b = buckets.get(str(hour)) or {"n": 0, "sum": 0.0, "min": 1e9}
        buckets[str(hour)] = {"n": b["n"] + 1, "sum": round(b["sum"] + sample, 3), "min": round(min(b["min"], sample), 3)}
        for k in [k for k in buckets if int(k) < hour - 24]:
            del buckets[k]
        means = sorted(v["sum"] / v["n"] for v in buckets.values() if v["n"])
        mid = len(means) // 2
        learned["baseline_gib"] = round(means[mid] if len(means) % 2 else (means[mid - 1] + means[mid]) / 2, 2)
        learned["samples"] = learned.get("samples", 0) + 1
        state.REG.dirty = True

    def apply_spec(self, spec):
        """Fakten/Policy eines registrierten Knotens uebernehmen (Registrierung oder Policy-Aenderung in der UI)."""
        if spec.get("vram_total_gib"):
            self.vram_total_gib = float(spec["vram_total_gib"])
        if spec.get("mac"):
            self.mac = spec["mac"]
        self.wol = bool(spec.get("wol", self.wol))
        self.weight = int(spec.get("weight", self.weight))
        if spec.get("gpu"):
            self.gpu = spec["gpu"]
        if spec.get("foreign_vram_baseline_gib") is not None:
            self.foreign_baseline_gib = float(spec["foreign_vram_baseline_gib"])
        self.max_inflight = int(spec["max_inflight"]) if spec.get("max_inflight") else None
        self.busy_util_pct = float(spec["busy_gpu_util_pct"]) if spec.get("busy_gpu_util_pct") is not None else None
        self.busy_foreign_pol = float(spec["busy_foreign_gib"]) if spec.get("busy_foreign_gib") is not None else None
        self.guard_policy = bool(spec.get("gpu_guard", True))   # Policy je Knoten: Router beachtet den GPU-Schutz-Status (Deckel, Score, HA)

    def busy_util_threshold(self):
        return self.busy_util_pct if self.busy_util_pct is not None else state.CFG.busy_util

    def busy_foreign_threshold(self):
        return self.busy_foreign_pol if self.busy_foreign_pol is not None else state.CFG.busy_foreign_gib

    # --- Stufe 3: Circuit Breaker / Admission ---
    def effective_max_inflight(self):
        base = self.max_inflight or state.CFG.admission["max_inflight_default"]
        g = state.CFG.gpu_guard
        if not g["enabled"] or not self.guard_policy:
            return base
        gs = self.guard_state()
        if gs == "gedrosselt" or (gs is None and g["require_fresh_status"]):
            return min(base, g["throttled_max_inflight"])
        return base

    def guard_state(self, now=None):
        """Zustand des GPU-Schutzes laut Agent; None = kein oder veralteter Status (aelter als 30 s)."""
        if not self.guard or not self.guard_ts or (now or time.time()) - self.guard_ts > 30:
            return None
        return self.guard.get("state")

    def guard_view(self, now=None):
        """Fuer /admin/state, HA und UI: Status plus Policy und Frische."""
        g = self.guard or {}
        return {"state": self.guard_state(now), "reported": g.get("state"), "limit_w": g.get("limit_w"), "target_w": g.get("target_w"),
                "default_limit_w": g.get("default_limit_w"), "problem": bool(g.get("problem")), "grund": g.get("grund"),
                "warnungen": g.get("warnungen") or [], "quelle": g.get("quelle"), "hochlast_s": g.get("hochlast_s"),
                "policy": self.guard_policy, "router_enabled": state.CFG.gpu_guard["enabled"]}

    def breaker_allows(self, now):
        if self.breaker == "closed":
            return True
        if self.breaker == "open":
            if now < self.breaker_until:
                return False
            self.breaker = "half_open"
            log.info("node %s: Breaker half_open - eine Probe", self.name)
            state.remember({"event": "breaker", "node": self.name, "state": "half_open"})
        return self.inflight == 0   # half_open: genau eine Probe, solange nichts anderes laeuft

    def breaker_fail(self, now, why):
        cfg = state.CFG.breaker
        self.breaker_fails = [t for t in self.breaker_fails if now - t <= cfg["window_s"]] + [now]
        if self.breaker == "half_open" or len(self.breaker_fails) >= cfg["failures"]:
            self.breaker, self.breaker_until, self.breaker_fails = "open", now + cfg["open_s"], []
            log.warning("node %s: Breaker OPEN fuer %.0fs (%s)", self.name, cfg["open_s"], why)
            state.remember({"event": "breaker", "node": self.name, "state": "open", "reason": why})

    def breaker_ok(self):
        if self.breaker != "closed":
            self.breaker = "closed"
            log.info("node %s: Breaker closed (Probe erfolgreich)", self.name)
            state.remember({"event": "breaker", "node": self.name, "state": "closed"})
        self.breaker_fails = []

    def breaker_reset(self):
        self.breaker, self.breaker_fails, self.breaker_until = "closed", [], 0.0

    def budget_gib(self, now, model):
        """Was Ollama auf diesem Knoten belegen darf: freies VRAM + alles, was Ollama selbst hält (wird bei Bedarf
        verdrängt oder per Runner-Sharing wiederverwendet) - Reserve. Fremdes VRAM (Desktop, Spiel) zählt nicht."""
        reserve = state.CFG.reserve.get(self.state, 1.0)
        if self.gpu_known(now) and self.vram_free_gib is not None:
            return self.vram_free_gib + self.ollama_vram_gib() - reserve
        return self.vram_total_gib - reserve

    def snapshot(self, now):
        return {
            "state": self.state, "busy_reason": self.busy_reason,
            "models": sorted(self.models), "loaded": {k: round(v, 2) for k, v in self.loaded.items()},
            "inflight": self.inflight, "gpu_known": bool(self.gpu_known(now)),
            "gpu_util": self.gpu_util, "vram_free_gib": self.vram_free_gib,
            "vram_used_gib": self.vram_used_gib, "vram_total_gib": self.vram_total_gib, "foreign_vram_gib": round(self.foreign_vram_gib(), 2),
            "heartbeat_age_s": round(now - self.hb_ts, 1) if self.hb_ts else None,
            "wol": self.wol, "tunnel": self.tunnel is not None, "tls": self.tls, "fingerprint": self.fp[:16] if self.fp else None, "baseline_gib": round(self.baseline_gib(), 2), "weight": self.weight, "tls_fingerprint": self.tls_fp[:16] if self.tls_fp else None, "last_wake_age_s": round(now - self.last_wake, 1) if self.last_wake else None,
            "gpu": self.gpu, "sensors": self.sensors, "gpu_guard": self.guard_view(now), "loaded_names_by_digest": sorted(m for m in self.models if self.is_loaded(m)),
            "breaker": self.breaker, "max_inflight": self.effective_max_inflight(), "draining": now < self.draining_until,
        }
