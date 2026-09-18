"""Regel-Engine: Stichwort-Treffer je Option, deterministisch, ohne Abhaengigkeiten.

Dient als Vergleichsbasis ("Rules" in der Messreihe) und als letztes Glied der Kette, wenn alles andere ausfaellt oder
unsicher ist. Die Stichwoerter kommen aus `decision_engine.rules` (Option -> Liste); fehlt eine Option dort, bekommt sie
nur die Grundgewichtung. Die Verteilung ist eine Softmax ueber die Trefferzahl - eine Heuristik, keine Wahrscheinlichkeit
im statistischen Sinn, darum bleibt `calibrated_confidence` leer.
"""

from __future__ import annotations

import math
import re
import time

from .base import DecisionEngine, DecisionRequest, DecisionResult, normalize

DEFAULT_RULES = {
    "code": ["code", "funktion", "function", "python", "javascript", "typescript", "bash", "powershell", "sql", "regex", "bug",
             "fehler in", "stack trace", "traceback", "compile", "refactor", "unit test", "klasse ", "class ", "def ", "import ",
             "```", "skript", "script", "yaml", "json", "dockerfile", "api", "endpoint", "implementier", "programm"],
    "assist": ["schalte", "mach das licht", "licht an", "licht aus", "rollladen", "jalousie", "heizung", "temperatur", "thermostat",
               "wecker", "timer", "erinnere", "einkaufsliste", "todo", "wie spaet", "wie spät", "wetter", "garage", "tor auf",
               "tor zu", "staubsauger", "musik", "lautstaerke", "lautstärke", "szene", "dimm", "steckdose", "turn on", "turn off",
               "set the", "alexa", "hey"],
    "gross": ["analysiere", "vergleiche", "ausfuehrlich", "ausführlich", "detailliert", "strategie", "konzept", "abwaeg", "abwäg",
              "pro und contra", "vor- und nachteile", "essay", "gutachten", "recherch", "beweis", "herleit", "mehrstufig",
              "schritt fuer schritt", "schritt für schritt", "plane", "roadmap", "architektur", "in depth", "comprehensive",
              "thorough", "trade-off", "tradeoff"],
    "standard": ["fasse zusammen", "zusammenfassung", "uebersetze", "übersetze", "erklaere", "erkläre", "was ist", "wer ist",
                 "formuliere", "schreib eine mail", "korrigiere", "umschreib", "kurz", "liste", "summarize", "translate", "explain"],
}
LONG_CONTEXT_CHARS = 1500   # ab hier zaehlt der Umfang als Indiz fuer die grosse Rolle


class RulesEngine(DecisionEngine):
    name = "rules"

    def __init__(self, rules: dict | None = None, temperature: float = 0.8, base_option: str | None = None):
        self.rules = {k: [w.lower() for w in v] for k, v in (rules or DEFAULT_RULES).items()}
        self.temperature = temperature
        self.base_option = base_option   # bekommt bei null Treffern den Zuschlag (z. B. standard)

    async def decide(self, req: DecisionRequest) -> DecisionResult:
        t0 = time.perf_counter()
        text = req.context.lower()
        scores = {o: self._score(o, text, req) for o in req.options}
        probs = normalize(_softmax(scores, self.temperature), req.options)
        selected = max(probs, key=probs.get) if probs else None
        return DecisionResult(selected=selected, probabilities=probs, engine=self.name, model="keywords-v1",
                              latency_ms=(time.perf_counter() - t0) * 1000, model_probability=probs.get(selected) if selected else None,
                              metadata={"hits": {o: s for o, s in scores.items() if s}})

    def _score(self, option: str, text: str, req: DecisionRequest) -> float:
        hits = sum(1 for w in self.rules.get(option, []) if w in text)
        if option == "gross" and len(req.context) > LONG_CONTEXT_CHARS:
            hits += 1
        if option == "code" and re.search(r"\b(def|class|import|function|const|let|SELECT|FROM)\b", req.context):
            hits += 1
        if option == "assist" and len(req.context) < 60 and not req.metadata.get("tools"):
            hits += 0.5   # kurze Saetze ohne Werkzeuge: eher Sprachbefehl
        if req.metadata.get("tools") and option in ("code", "standard"):
            hits += 0.5
        if option == self.base_option:
            hits += 0.75
        return float(hits)


def _softmax(scores: dict[str, float], temperature: float) -> dict[str, float]:
    if not scores:
        return {}
    m = max(scores.values())
    exps = {k: math.exp((v - m) / max(temperature, 1e-6)) for k, v in scores.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}
