"""Decision Engine - base types, provider-agnostic.

A Decision Engine answers a selection question: given a context (user text) and options (roles in Skirnir), which option fits? It returns a distribution over the options, never an assignment to a node - that remains the existing path (role -> tiers -> capabilities -> availability -> score).

Terms (Section 7 of the proposal): `model_probability` is the raw model output. `calibrated_confidence` is None until a measured calibration (temperature from the validation set, decision-eval) exists for this specific model.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass, field

from ..outcome import Problem, unavailable

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
    probabilities: dict[str, float]          # full distribution over the options (sum 1, or empty)
    engine: str
    model: str | None
    latency_ms: float
    model_probability: float | None = None   # p(selected) roh - KEINE kalibrierte Konfidenz
    calibrated_confidence: float | None = None
    uncertainty: dict = field(default_factory=dict)   # margin, entropy_ratio, reasons (for the fallback)
    fallback: list[str] = field(default_factory=list)  # Engines that were previously uncertain/failed
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


class DecisionEngine(abc.ABC):
    """Interface. Implementations: rules, jevlike, local_llm, tfidf.

    decide() returns a DecisionResult or a Problem (outcome.py) if the engine is unreachable or its response was unusable - then the chain asks the next engine."""

    name = "abstract"

    @abc.abstractmethod
    async def decide(self, req: DecisionRequest) -> DecisionResult | Problem:
        ...

    async def health(self) -> dict:
        return {"ok": True}


def engine_failed(message: str) -> Problem:
    """Engine unreachable or response unusable -> next engine in the chain."""
    return unavailable(message, "engine")


def normalize(probabilities: dict[str, float], options: list[str]) -> dict[str, float]:
    """Only known options, no negative values, sum 1. Empty if nothing usable comes (even for non-numeric values - the distribution comes from an external service)."""
    if not isinstance(probabilities, dict):
        return {}
    values = {o: probabilities.get(o) or 0.0 for o in options}
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values.values()):
        return {}
    clean = {o: max(0.0, float(v)) for o, v in values.items()}
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {o: v / total for o, v in clean.items()}


def uncertainty_of(probabilities: dict[str, float]) -> dict:
    """Metrics for the fallback decision: distance between rank 1 and 2 and normalized entropy (0 = certain, 1 = uniform).
    Absolute probabilities alone are misleading (Section 8): 0.46 vs 0.43 is uncertain, 0.82 vs 0.07 is not."""
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
    """Empty = decision stands. Otherwise the reasons why the next engine in the chain is asked."""
    reasons = []
    if unc.get("top", 0.0) < policy.get("min_top_probability", 0.0):
        reasons.append(f"top {unc.get('top')} < {policy['min_top_probability']}")
    if unc.get("margin", 0.0) < policy.get("min_margin", 0.0):
        reasons.append(f"margin {unc.get('margin')} < {policy['min_margin']}")
    if unc.get("entropy_ratio", 1.0) > policy.get("max_entropy_ratio", 1.0):
        reasons.append(f"entropy {unc.get('entropy_ratio')} > {policy['max_entropy_ratio']}")
    return reasons


def apply_temperature(probabilities: dict[str, float], temperature: float) -> dict[str, float]:
    """Temperature scaling (calibration): p_i^(1/T) renormalized. T > 1 flattens the distribution."""
    if not probabilities or temperature <= 0 or abs(temperature - 1.0) < 1e-9:
        return dict(probabilities)
    scaled = {k: (v ** (1.0 / temperature)) if v > 0 else 0.0 for k, v in probabilities.items()}
    total = sum(scaled.values())
    return {k: v / total for k, v in scaled.items()} if total > 0 else dict(probabilities)
