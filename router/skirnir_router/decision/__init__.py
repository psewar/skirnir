"""Decision Engine (2026-09-16): semantische Vorentscheidung fuer die Auto-Rolle, getrennt vom generativen Weg.

Ein Client, der `auto:latest` (Name konfigurierbar) anfragt, bekommt die Rolle von einer Kette von Engines gewaehlt:
    decision_engine.chain: [jevlike, rules]      # erste sichere Antwort gewinnt, sonst die naechste Engine
Jede Engine liefert eine Verteilung ueber die Optionen (= Rollennamen). Ob sie "sicher" ist, entscheidet nicht die
absolute Wahrscheinlichkeit allein, sondern Abstand Platz 1/2 und normierte Entropie (`policy`). Ist auch die letzte
Engine unsicher oder ausgefallen, gilt `default`. Danach laeuft alles wie bei jeder Rolle: Stufen, Faehigkeiten,
Verfuegbarkeit, Score. Die Engine waehlt nie einen Knoten.

Messbarkeit: Prometheus (`skirnir_decision_total`, `_latency_seconds`, `_fallback_total`), Entscheidungsprotokoll
(`event: decision`), `routing.decision` in der Antwort (bei explizitem routing-Block), optional Trainingsdaten (capture).
"""

from __future__ import annotations

import json
import os

from .. import metrics, state
from ..common import log
from .base import DecisionRequest, DecisionResult, EngineError, apply_temperature, uncertain_reasons, uncertainty_of
from .capture import Capture
from .jevlike import JevlikeEngine
from .local_llm import LocalLLMEngine
from .rules import RulesEngine
from .tfidf import TfidfEngine

DEFAULT_POLICY = {"min_top_probability": 0.5, "min_margin": 0.2, "max_entropy_ratio": 0.75}
LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)
DECIDER = None   # Instanz nach init(); None = Auto-Rolle aus


def init():
    """Aus state.CFG.decision bauen (beim Start). Engines entstehen einmal; Policy/Default liest decide() live."""
    global DECIDER
    cfg = state.CFG.decision
    DECIDER = Decider(cfg) if cfg.get("enabled") else None
    if DECIDER:
        log.info("decision engine: Rolle %s, Optionen %s, Kette %s, default %s", DECIDER.exposed, DECIDER.options,
                 [e.name for e in DECIDER.chain], DECIDER.default)
    return DECIDER


def ensure():
    """Nach einem Konfig-Reload (UI-Schalter, SIGHUP): Engines bauen, wenn die Auto-Rolle jetzt an ist und noch keine
    Instanz besteht. Eine bestehende Instanz bleibt (Kette/Endpunkte aendern sich nur mit Neustart), active() liest das Flag live."""
    if DECIDER is None and state.CFG.decision.get("enabled"):
        init()
    return DECIDER


def active():
    return DECIDER if (DECIDER and state.CFG.decision.get("enabled")) else None


def exposed_for(option):
    """Option (Rollen-Kurzname) -> Name, unter dem die Rolle angefragt wird (z. B. code -> code:latest)."""
    return next((exposed for exposed, r in state.CFG.roles.items() if r["name"] == option), None)


def context_from(body: dict, path: str, limit: int) -> tuple[str, dict]:
    """Der Text, ueber den entschieden wird: letzte Benutzernachricht (chat) oder prompt (generate), gekuerzt; dazu
    Hinweise als Metadaten (Werkzeuge, Bilder, System-Prompt), die auch als Text angehaengt werden - ein Byte-Encoder
    sieht nur den Anfang, darum steht der Benutzertext vorn."""
    msgs = body.get("messages") or []
    user = [m for m in msgs if isinstance(m, dict) and m.get("role") == "user"]
    text = _content_text(user[-1].get("content")) if user else str(body.get("prompt") or "")
    meta = {"tools": len(body.get("tools") or []), "images": sum(len(m.get("images") or []) for m in msgs if isinstance(m, dict)) + len(body.get("images") or []),
            "messages": len(msgs), "system": any(isinstance(m, dict) and m.get("role") == "system" for m in msgs) or bool(body.get("system")),
            "path": path}
    hints = " ".join(f"[{k}={v}]" for k, v in (("tools", meta["tools"]), ("images", meta["images"])) if v)
    ctx = text.strip()[:limit]
    return (f"{ctx} {hints}".strip() if hints else ctx), meta


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):   # OpenAI-Teile: [{"type": "text", "text": ...}, ...]
        return " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or "")


class Decider:
    def __init__(self, cfg: dict):
        self.role = cfg.get("role") or "auto"
        self.exposed = f"{self.role}:latest"
        self.options = list(cfg.get("options") or [])
        self.context_chars = int(cfg.get("context_chars") or 512)
        self.engines = self._build_engines(cfg)
        self.chain = [self.engines[n] for n in (cfg.get("chain") or ["rules"]) if n in self.engines]
        self.capture = Capture(cfg.get("capture") or {})
        self.calibration = _load_calibration(cfg.get("calibration_path"))

    # -- Konfiguration, live --

    @property
    def default(self):
        return state.CFG.decision.get("default") or (self.options[0] if self.options else None)

    @property
    def policy(self):
        return {**DEFAULT_POLICY, **{k: v for k, v in (state.CFG.decision.get("policy") or {}).items() if v is not None}}

    def _build_engines(self, cfg):
        engines = {"rules": RulesEngine(cfg.get("rules") or None, base_option=cfg.get("default"))}
        jl = cfg.get("jevlike") or {}
        if jl.get("endpoint"):
            engines["jevlike"] = JevlikeEngine(jl["endpoint"], jl.get("model"), float(jl.get("timeout_s") or 2.0))
        ll = cfg.get("local_llm") or {}
        if ll.get("model"):
            engines["local_llm"] = LocalLLMEngine(ll["model"], float(ll.get("timeout_s") or 20.0), ll.get("descriptions") or {})
        em = cfg.get("embed") or {}   # Stufe 2: Embedding-Dienst (decision-embed), gleiches Protokoll wie jevlike
        if em.get("endpoint"):
            engines["embed"] = JevlikeEngine(em["endpoint"], em.get("model"), float(em.get("timeout_s") or 2.0), name="embed")
        tf = cfg.get("tfidf") or {}
        if tf.get("model_path"):
            try:
                engines["tfidf"] = TfidfEngine(tf["model_path"])
            except (OSError, ValueError, KeyError) as e:   # Modell fehlt/kaputt: Engine faellt aus der Kette, Router startet trotzdem
                log.warning("decision tfidf: Modell %s nicht ladbar: %s", tf["model_path"], e)
        return engines

    # -- Entscheiden --

    async def decide_body(self, body: dict, path: str, client: str | None, request_id: str) -> DecisionResult:
        context, meta = context_from(body, path, self.context_chars)
        req = DecisionRequest(context=context, options=list(self.options), metadata=meta)
        result = await self.decide(req, engines=None)
        self._observe(result, client, request_id)
        self.capture.record(context, self.options, result.selected, "engine", client, request_id, result.as_dict())
        return result

    async def decide(self, req: DecisionRequest, engines=None) -> DecisionResult:
        """Kette abarbeiten. `engines` = explizite Liste (Auswertung), sonst die konfigurierte Kette."""
        policy = self.policy
        tried, last = [], None
        for engine in (engines if engines is not None else self.chain):
            try:
                result = await engine.decide(req)
            except EngineError as e:
                log.warning("decision %s: %s", engine.name, e)
                tried.append(f"{engine.name}: ausgefallen")
                metrics._counter("skirnir_decision_fallback_total", {"from": engine.name, "reason": "error"})
                continue
            self._calibrate(result)
            result.uncertainty = uncertainty_of(result.probabilities)
            reasons = uncertain_reasons(result.uncertainty, policy)
            if result.selected not in req.options:
                reasons.append("unbekannte Option")
            result.fallback = list(tried)
            if not reasons:
                return result
            result.uncertainty["reasons"] = reasons
            tried.append(f"{engine.name}: {reasons[0]}")
            metrics._counter("skirnir_decision_fallback_total", {"from": engine.name, "reason": reasons[0].split(" ")[0]})
            last = result
        if last is not None:   # alle unsicher: die letzte Engine entscheidet trotzdem (transparent markiert)
            last.fallback = tried
            last.uncertainty["forced"] = True
            return last
        return DecisionResult(selected=self.default, probabilities={o: (1.0 if o == self.default else 0.0) for o in req.options},
                              engine="default", model=None, latency_ms=0.0, fallback=tried, uncertainty={"forced": True})

    def _calibrate(self, result: DecisionResult):
        cal = self.calibration.get(f"{result.engine}/{result.model}") or self.calibration.get(result.engine)
        if not cal or not result.probabilities:
            return
        result.metadata["raw_probabilities"] = dict(result.probabilities)
        result.probabilities = apply_temperature(result.probabilities, float(cal.get("temperature", 1.0)))
        result.selected = max(result.probabilities, key=result.probabilities.get)
        result.calibrated_confidence = result.probabilities[result.selected]
        result.metadata["calibration"] = {k: cal[k] for k in ("temperature", "measured_at", "ece_after") if k in cal}

    def _observe(self, result: DecisionResult, client, request_id):
        metrics._counter("skirnir_decision_total", {"engine": result.engine, "selected": result.selected or "-", "fallback": "1" if result.fallback else "0"})
        metrics._hist("skirnir_decision_latency_seconds", {"engine": result.engine}, LATENCY_BUCKETS, result.latency_ms / 1000.0)
        state.remember({"event": "decision", "request_id": request_id, "client": client, "engine": result.engine, "model": result.model,
                        "selected": result.selected, "top": result.uncertainty.get("top"), "margin": result.uncertainty.get("margin"),
                        "fallback": result.fallback, "latency_ms": round(result.latency_ms, 1)})

    # -- Labels aus echten Client-Entscheidungen --

    def record_client_choice(self, body: dict, path: str, role_name: str, client: str | None, request_id: str):
        """Client hat eine konkrete Rolle aus den Optionen verlangt -> echtes Label fuer den Trainingssatz (Opt-in)."""
        if role_name not in self.options or not self.capture.allowed(client):
            return
        context, _ = context_from(body, path, self.context_chars)
        self.capture.record(context, self.options, role_name, "client", client, request_id)

    async def status(self) -> dict:
        health = {}
        for name, e in self.engines.items():
            health[name] = await e.health()
        return {"enabled": True, "role": self.exposed, "options": self.options, "chain": [e.name for e in self.chain],
                "default": self.default, "policy": self.policy, "engines": health, "calibration": sorted(self.calibration),
                "capture": {"enabled": self.capture.enabled, "clients": sorted(self.capture.clients), "path": self.capture.path}}


def _load_calibration(path) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.warning("decision calibration %s: %s", path, e)
        return {}
