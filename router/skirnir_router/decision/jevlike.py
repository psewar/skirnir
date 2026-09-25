"""Adapter fuer Entscheidungsdienste im eigenen Prozess bzw. Container, lokal per HTTP: Jevlike (decision-jevlike/server.py)
und der Embedding-Dienst der Stufe 2 (decision-embed/server.py) sprechen dasselbe Protokoll.

Der Router kennt nur dieses Protokoll:
    POST {endpoint}/decide   {"context": "...", "options": ["a", "b"]}
    ->   {"probabilities": {"a": 0.8, "b": 0.2}, "model": "skirnir-router-v1", "encoder": "tiny", "latency_ms": 3.1}
    GET  {endpoint}/health   -> {"ok": true, "model": ..., "encoder": ..., "offline": true}
Nichts von Jevlikes Datenstrukturen (Checkpoints, Collator, Tensoren) taucht hier auf.
"""

from __future__ import annotations

import time

from aiohttp import ClientError, ClientTimeout

from .. import state
from .base import DecisionEngine, DecisionRequest, DecisionResult, EngineError, normalize


class JevlikeEngine(DecisionEngine):
    name = "jevlike"

    def __init__(self, endpoint: str, model: str | None = None, timeout_s: float = 2.0, name: str | None = None):
        if name:
            self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = ClientTimeout(total=timeout_s)

    async def decide(self, req: DecisionRequest) -> DecisionResult:
        t0 = time.perf_counter()
        body = {"context": req.context, "options": list(req.options)}
        if self.model:
            body["model"] = self.model
        try:
            async with state.SESSION.post(self.endpoint + "/decide", json=body, timeout=self.timeout) as r:
                if r.status != 200:
                    raise EngineError(f"{self.name} HTTP {r.status}: {(await r.text())[:120]}")
                data = await r.json(content_type=None)
        except (ClientError, TimeoutError, OSError) as e:
            raise EngineError(f"{self.name} nicht erreichbar: {type(e).__name__}: {e}") from e
        probs = normalize(data.get("probabilities") or {}, req.options)
        if not probs:
            raise EngineError(f"{self.name}: keine verwertbare Verteilung")
        selected = max(probs, key=probs.get)
        return DecisionResult(selected=selected, probabilities=probs, engine=self.name, model=data.get("model") or self.model,
                              latency_ms=(time.perf_counter() - t0) * 1000, model_probability=probs[selected],
                              metadata={"encoder": data.get("encoder"), "service_latency_ms": data.get("latency_ms")})

    async def health(self) -> dict:
        try:
            async with state.SESSION.get(self.endpoint + "/health", timeout=ClientTimeout(total=2)) as r:
                data = await r.json(content_type=None)
                return {"ok": r.status == 200 and bool(data.get("ok")), **{k: v for k, v in data.items() if k != "ok"}}
        except (ClientError, TimeoutError, OSError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
