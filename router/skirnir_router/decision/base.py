"""Decision Engine - Grundtypen, unabhaengig von jedem Anbieter.

Eine Decision Engine beantwortet eine Auswahlfrage: gegeben ein Kontext (der Benutzertext) und Optionen (bei Skirnir die
Rollen), welche Option passt? Sie liefert eine Verteilung ueber die Optionen, nie eine Zuweisung an einen Knoten - das
bleibt der bestehende Weg (Rolle -> Stufen -> Faehigkeiten -> Verfuegbarkeit -> Score).

Begriffe (Abschnitt 7 des Vorschlags): `model_probability` ist die rohe Ausgabe des Modells. `calibrated_confidence` ist
None, bis eine gemessene Kalibrierung (Temperatur aus dem Validierungssatz, decision-eval) fuer genau dieses Modell vorliegt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

KINDS = ("choice", "score", "boolean")


@dataclass
class DecisionRequest:
    context: str
    options: list[str]
    question: str = "Welche Rolle passt zu dieser Anfrage?"
    kind: str = "choice"
    metadata: dict = field(default_factory=dict)


@dataclass
class DecisionResult:
    selected: str | None
    probabilities: dict[str, float]          # vollstaendige Verteilung ueber die Optionen (Summe 1, oder leer)
    engine: str
    model: str | None
    latency_ms: float
    model_probability: float | None = None   # p(selected) roh - KEINE kalibrierte Konfidenz
    calibrated_confidence: float | None = None
    uncertainty: dict = field(default_factory=dict)   # margin, entropy_ratio, reasons (fuer den Fallback)
    fallback: list[str] = field(default_factory=list)  # Engines, die vorher unsicher/ausgefallen waren
    metadata: dict = field(default_factory=dict)

    def ranked(self):
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])

    def top(self, n=3):
        return [k for k, _ in self.ranked()[:n]]

    def as_dict(self):
        return {"selected": self.selected, "probabilities": {k: round(v, 4) for k, v in self.probabilities.items()},
                "model_probability": None if self.model_probability is None else round(self.model_probability, 4),
                "calibrated_confidence": None if self.calibrated_confidence is None else round(self.calibrated_confidence, 4),
                "engine": self.engine, "model": self.model, "latency_ms": round(self.latency_ms, 1),
                "uncertainty": self.uncertainty, "fallback": list(self.fallback), **({"metadata": self.metadata} if self.metadata else {})}


class DecisionEngine:
    """Schnittstelle. Implementierungen: rules, jevlike, local_llm (spaeter typesafe-jev ...)."""

    name = "abstract"

    async def decide(self, req: DecisionRequest) -> DecisionResult:
        raise NotImplementedError

    async def health(self) -> dict:
        return {"ok": True}


class EngineError(Exception):
    """Engine nicht erreichbar oder Antwort unbrauchbar -> naechste Engine der Kette."""


def normalize(probabilities: dict[str, float], options: list[str]) -> dict[str, float]:
    """Nur bekannte Optionen, keine negativen Werte, Summe 1. Leer, wenn nichts Verwertbares kommt."""
    clean = {o: max(0.0, float(probabilities.get(o, 0.0) or 0.0)) for o in options}
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {o: v / total for o, v in clean.items()}


def uncertainty_of(probabilities: dict[str, float]) -> dict:
    """Kennzahlen fuer die Fallback-Entscheidung: Abstand Platz 1 zu 2 und normierte Entropie (0 = sicher, 1 = gleichverteilt).
    Absolute Wahrscheinlichkeiten allein taeuschen (Abschnitt 8): 0,46 zu 0,43 ist unsicher, 0,82 zu 0,07 nicht."""
    ranked = sorted(probabilities.values(), reverse=True)
    if not ranked:
        return {"top": 0.0, "margin": 0.0, "entropy_ratio": 1.0}
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else 0.0
    n = len(ranked)
    entropy = -sum(p * math.log(p) for p in ranked if p > 0)
    ratio = entropy / math.log(n) if n > 1 else 0.0
    return {"top": round(top, 4), "margin": round(top - second, 4), "entropy_ratio": round(ratio, 4)}


def uncertain_reasons(unc: dict, policy: dict) -> list[str]:
    """Leer = Entscheidung gilt. Sonst die Gruende, warum die naechste Engine der Kette gefragt wird."""
    reasons = []
    if unc.get("top", 0.0) < policy.get("min_top_probability", 0.0):
        reasons.append(f"top {unc.get('top')} < {policy['min_top_probability']}")
    if unc.get("margin", 0.0) < policy.get("min_margin", 0.0):
        reasons.append(f"margin {unc.get('margin')} < {policy['min_margin']}")
    if unc.get("entropy_ratio", 1.0) > policy.get("max_entropy_ratio", 1.0):
        reasons.append(f"entropy {unc.get('entropy_ratio')} > {policy['max_entropy_ratio']}")
    return reasons


def apply_temperature(probabilities: dict[str, float], temperature: float) -> dict[str, float]:
    """Temperatur-Skalierung (Kalibrierung): p_i^(1/T) renormiert. T > 1 macht die Verteilung flacher."""
    if not probabilities or temperature <= 0 or abs(temperature - 1.0) < 1e-9:
        return dict(probabilities)
    scaled = {k: (v ** (1.0 / temperature)) if v > 0 else 0.0 for k, v in probabilities.items()}
    total = sum(scaled.values())
    return {k: v / total for k, v in scaled.items()} if total > 0 else dict(probabilities)
