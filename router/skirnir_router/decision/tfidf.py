"""Engine "tfidf": character n-grams + logistic regression, pure standard library, runs in the router process.

Stage 1 of the recommendation from 2026-09-17: real text classification instead of keyword lists, no torch, no service, under
one millisecond per request. Features: character n-grams (3-5) within words with boundary marking, plus word uni- and
bigrams; TF-IDF weighting with L2 normalization; classifier: Softmax regression (weights per class). Training is in
decision-eval/train_tfidf.py (numpy), the model is a JSON file (vocabulary, IDF, weights, bias, classes).

The probabilities are Softmax outputs of a linear model - usable for ranking and
uncertainty mass, but not calibrated confidence (calibration.json, like the other engines).
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter

from ..outcome import Problem, failed
from .base import DecisionEngine, DecisionRequest, DecisionResult, engine_failed, normalize

WORD = re.compile(r"[a-z0-9äöüß]+|[^\sa-z0-9äöüß]", re.I)
NGRAM_RANGE = (3, 5)


def features(text: str) -> Counter:
    """Feature counter for a text (same function during training and in the router)."""
    text = text.lower()
    tokens = WORD.findall(text)
    feats = Counter()
    for tok in tokens:
        padded = f"<{tok}>"
        for n in range(NGRAM_RANGE[0], NGRAM_RANGE[1] + 1):
            for i in range(0, max(1, len(padded) - n + 1)):
                feats["c:" + padded[i:i + n]] += 1
        feats["w:" + tok] += 1
    for a, b in zip(tokens, tokens[1:], strict=False):   # Bigramme: absichtlich um eins versetzt
        feats["b:" + a + " " + b] += 1
    feats["len:" + _length_bucket(len(text))] += 1
    return feats


def _length_bucket(n: int) -> str:
    for edge in (40, 80, 160, 320, 640, 1280):
        if n <= edge:
            return str(edge)
    return "long"


def vectorize(feats: Counter, vocab: dict, idf: list) -> dict:
    """Sparse TF-IDF-Vektor (Index -> Gewicht), L2-normiert; unbekannte Merkmale fallen weg."""
    vec = {}
    for f, c in feats.items():
        j = vocab.get(f)
        if j is not None:
            vec[j] = (1 + math.log(c)) * idf[j]
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {j: v / norm for j, v in vec.items()}


MODEL_KEYS = ("classes", "vocab", "idf", "weights", "bias")


def load_model(path: str) -> TfidfModel | Problem:
    """Read model file: the model or a Problem (missing, unreadable, incomplete) - then the engine drops out of the chain."""
    if not os.path.exists(path):
        return engine_failed(f"{path} missing")
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as e:  # exception-ok: file system and the model file's JSON
        return engine_failed(f"{path}: {e}")
    if not isinstance(m, dict) or any(k not in m for k in MODEL_KEYS) or not isinstance(m["classes"], list):
        return engine_failed(f"{path}: model incomplete (needs {', '.join(MODEL_KEYS)})")
    return TfidfModel(m, path)


class TfidfModel:
    def __init__(self, m: dict, path: str):
        self.name = m.get("name") or os.path.splitext(os.path.basename(path))[0]
        self.classes = list(m["classes"])
        self.vocab = m["vocab"]                       # Merkmal -> Spaltenindex
        self.idf = m["idf"]
        self.weights = m["weights"]                   # [Klasse][Spalte]
        self.bias = m["bias"]
        self.meta = {k: m[k] for k in ("trained_at", "n_train", "validation_top1", "test_top1") if k in m}

    def predict(self, text: str) -> dict[str, float]:
        vec = vectorize(features(text), self.vocab, self.idf)
        logits = []
        for ci in range(len(self.classes)):
            w = self.weights[ci]
            logits.append(self.bias[ci] + sum(w[j] * v for j, v in vec.items()))
        mx = max(logits)
        exps = [math.exp(z - mx) for z in logits]
        total = sum(exps)
        return {c: e / total for c, e in zip(self.classes, exps, strict=True)}


class TfidfEngine(DecisionEngine):
    name = "tfidf"

    def __init__(self, model: TfidfModel, model_path: str):
        self.path = model_path
        self.model = model

    @classmethod
    def load(cls, model_path: str) -> TfidfEngine | Problem:
        model = load_model(model_path)
        return model if failed(model) else cls(model, model_path)

    async def decide(self, req: DecisionRequest) -> DecisionResult:
        t0 = time.perf_counter()
        raw = self.model.predict(req.context)
        # Options the model does not know get 0; known ones are renormalized to the requested options
        probs = normalize({o: raw.get(o, 0.0) for o in req.options}, req.options)
        selected = max(probs, key=probs.get) if probs else None
        return DecisionResult(selected=selected, probabilities=probs, engine=self.name, model=self.model.name,
                              latency_ms=(time.perf_counter() - t0) * 1000, model_probability=probs.get(selected) if selected else None,
                              metadata={"unknown_options": [o for o in req.options if o not in self.model.classes]} if any(o not in self.model.classes for o in req.options) else {})

    async def health(self) -> dict:
        return {"ok": True, "model": self.model.name, "classes": self.model.classes, "vocab": len(self.model.vocab), **self.model.meta}
