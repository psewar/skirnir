"""Engine "local_llm": ein kleines lokales Sprachmodell klassifiziert ueber den eigenen Router (Rolle oder konkretes Modell),
mit erzwungenem JSON-Schema, ohne Denken, wenige Tokens. Liefert genau eine Option - die Verteilung ist deshalb
einpunktig und `model_probability` bleibt None (ein generatives Modell gibt keine Wahrscheinlichkeit ueber Optionen aus).

Laeuft durch /api/chat des Routers selbst (Identitaet skirnir-ui wie der Ausprobieren-Knopf), damit Admission, Breaker und
Metriken greifen. Kostet eine Modellantwort (0,3-1 s warm) - darum sinnvoll als Fallback fuer unsichere Faelle, nicht als
erste Stufe.
"""

from __future__ import annotations

import json
import time

from aiohttp import ClientError, ClientTimeout

from .. import state
from ..common import split_listen
from ..outcome import Problem
from .base import DecisionEngine, DecisionRequest, DecisionResult, engine_failed

PROMPT = ("Du ordnest eine Benutzeranfrage genau einer Kategorie zu. Antworte nur mit JSON.\n"
          "Kategorien: {options}\n"
          "{descriptions}"
          "Anfrage:\n\"\"\"\n{context}\n\"\"\"")


def chosen_option(content, options):
    """Die Option aus der Modellantwort: das Feld `option` des erzwungenen JSON, sonst die erste Option im Klartext."""
    try:
        parsed = json.loads(content)
    except ValueError:  # exception-ok: the model's output is untrusted JSON; whether it parses shows only the parser
        parsed = None
    if isinstance(parsed, dict):
        return parsed.get("option")
    return next((o for o in options if o in content), None)


class LocalLLMEngine(DecisionEngine):
    name = "local_llm"

    def __init__(self, model: str = "assist:latest", timeout_s: float = 20.0, descriptions: dict | None = None):
        self.model = model
        self.timeout = ClientTimeout(total=timeout_s)
        self.descriptions = descriptions or {}

    def _url(self):
        _, port = split_listen(state.CFG.listen)
        return f"{'https' if state.CFG.api_tls else 'http'}://127.0.0.1:{port}/api/chat"

    async def decide(self, req: DecisionRequest) -> DecisionResult | Problem:
        t0 = time.perf_counter()
        desc = "".join(f"- {o}: {self.descriptions[o]}\n" for o in req.options if o in self.descriptions)
        body = {"model": self.model, "stream": False, "think": False,
                "messages": [{"role": "user", "content": PROMPT.format(options=", ".join(req.options), descriptions=desc, context=req.context[:4000])}],
                "format": {"type": "object", "properties": {"option": {"type": "string", "enum": list(req.options)}}, "required": ["option"]},
                "options": {"num_predict": 40, "temperature": 0},
                "routing": {"priority": "interactive", "execution": "local", "request_id": f"decide-{int(t0 * 1000)}"}}
        hdrs = {"Authorization": "Bearer " + state.INTERNAL_TOKEN} if state.INTERNAL_TOKEN else {}
        try:
            async with state.SESSION.post(self._url(), json=body, headers=hdrs, timeout=self.timeout, ssl=False if state.CFG.api_tls else None) as r:
                data = await r.json(content_type=None)
                status = r.status
        except (ClientError, TimeoutError, OSError, ValueError) as e:  # exception-ok: network boundary of the self-call and its JSON
            return engine_failed(f"local_llm nicht erreichbar: {type(e).__name__}: {e}")
        if status != 200 or not isinstance(data, dict):
            return engine_failed(f"local_llm HTTP {status}: {str(data)[:120]}")
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        content = str(message.get("content") or "").strip()
        option = chosen_option(content, req.options)
        if option not in req.options:
            return engine_failed(f"local_llm: keine gueltige Option in {content[:80]!r}")
        probs = {o: (1.0 if o == option else 0.0) for o in req.options}
        return DecisionResult(selected=option, probabilities=probs, engine=self.name, model=(data.get("routing") or {}).get("model") or self.model,
                              latency_ms=(time.perf_counter() - t0) * 1000, model_probability=None,
                              metadata={"node": (data.get("routing") or {}).get("node"), "eval_count": data.get("eval_count")})
