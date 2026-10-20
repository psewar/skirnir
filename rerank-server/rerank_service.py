"""Kern des Reranker-Dienstes: Anfragepruefung, Zustand (starting/ok/selftest_failed), TEI-Antwortformat.

Unabhaengig von OpenVINO, damit die Selbsttests ohne GPU laufen: die Engine ist austauschbar
(`OpenVinoEngine` in engine_openvino.py, im Test eine Attrappe). Erwartete Faelle sind Werte (`Reply`), keine Exceptions
(AGENTS.md "Explicit control flow").
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field

STARTING, OK, SELFTEST_FAILED = "starting", "ok", "selftest_failed"
# Eindeutiges Paar fuer die Selbstpruefung: ein gesundes Modell gibt dem ersten Text deutlich mehr. 2026-10-20 lieferte
# ein beschaedigter Download (richtige Groesse, falsche Pruefsumme) genau hier Unsinn - der Dienst meldet sich erst danach gesund.
SELFTEST_QUERY = "Was ist die Hauptstadt von Deutschland?"
SELFTEST_TEXTS = ("Berlin ist die Hauptstadt Deutschlands.", "Bananen sind gelb und wachsen in warmen Ländern.")


@dataclass(frozen=True)
class Limits:
    max_texts: int = 512
    max_text_bytes: int = 8192
    max_body_bytes: int = 4 * 1024 * 1024
    max_query_bytes: int = 8192


@dataclass(frozen=True)
class Reply:
    status: int
    body: object


def error(status: int, reason: str) -> Reply:
    return Reply(status, {"error": {"reason": reason}})


def sigmoid(x: float) -> float:
    # Wie TEI ohne raw_scores: Relevanz 0..1. Grosse negative Logits ueberlaufen math.exp nicht, weil zuerst das Vorzeichen geprueft wird.
    return 1.0 / (1.0 + math.exp(-x)) if x >= 0 else math.exp(x) / (1.0 + math.exp(x))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def verify_manifest(model_dir: str) -> tuple[dict | None, str]:
    """(manifest, "") wenn alle Dateien laut manifest.json vorhanden sind und ihre SHA-256 stimmt, sonst (None, grund)."""
    path = os.path.join(model_dir, "manifest.json")
    if not os.path.isfile(path):
        return None, "manifest_missing"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    manifest = parse_json(text.encode())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        return None, "manifest_invalid"
    for name, expected in manifest["files"].items():
        file_path = os.path.join(model_dir, name)
        if not os.path.isfile(file_path):
            return None, f"file_missing:{name}"
        if sha256_file(file_path) != expected:
            return None, f"checksum_mismatch:{name}"
    return manifest, ""


_INVALID = object()


def parse_json(data: bytes):
    """Geparster Wert oder _INVALID. Untrusted JSON ist eine echte Grenze (AGENTS.md): hier und nur hier wird gefangen."""
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):  # exception-ok: Parsen fremder Eingabe ist nicht vorab pruefbar
        return _INVALID


def validate(body: bytes, limits: Limits) -> tuple[str, list[str]] | Reply:
    """(query, texts) oder eine Reply mit Status 400/413. Felder ausser query/texts werden ignoriert (TEI: return_text,
    raw_scores, truncate)."""
    if len(body) > limits.max_body_bytes:
        return error(413, "body_too_large")
    data = parse_json(body)
    if data is _INVALID or not isinstance(data, dict):
        return error(400, "invalid_json")
    query, texts = data.get("query"), data.get("texts")
    if not isinstance(query, str) or not query.strip():
        return error(400, "query_missing")
    if not isinstance(texts, list) or not texts:
        return error(400, "texts_missing")
    if not all(isinstance(t, str) for t in texts):
        return error(400, "texts_not_strings")
    if len(texts) > limits.max_texts:
        return error(413, "too_many_texts")
    if len(query.encode()) > limits.max_query_bytes:
        return error(413, "query_too_long")
    if any(len(t.encode()) > limits.max_text_bytes for t in texts):
        return error(413, "text_too_long")
    return query, texts


@dataclass
class Service:
    """Zustand und Ablauf. `engine_factory()` liefert (engine, grund): engine mit `.score(query, texts) -> list[float]`
    (Logits, je Text eins) und `.describe() -> dict`, oder (None, grund), wenn sie sich nicht bauen laesst."""

    engine_factory: object
    limits: Limits = field(default_factory=Limits)
    state: str = STARTING
    reason: str = ""
    engine: object = None
    info: dict = field(default_factory=dict)
    stats: dict = field(default_factory=lambda: {"requests": 0, "rejected": 0, "texts": 0, "busy_ms": 0.0})
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        engine, reason = self.engine_factory()
        if engine is None:
            self.state, self.reason = SELFTEST_FAILED, reason
            return
        scores = engine.score(SELFTEST_QUERY, list(SELFTEST_TEXTS))
        finite = len(scores) == 2 and all(isinstance(s, float) and math.isfinite(s) for s in scores)
        if not finite or scores[0] <= scores[1]:
            self.state, self.reason = SELFTEST_FAILED, f"selftest_pair:{scores}"
            return
        self.engine, self.info = engine, engine.describe()
        self.state, self.reason = OK, ""

    def health(self) -> Reply:
        if self.state != OK:
            return Reply(503, {"status": self.state, "reason": self.reason})
        return Reply(200, {"status": OK, "selftest": OK, **self.info, **self.stats})

    def rerank(self, body: bytes) -> Reply:
        if self.state != OK:
            return Reply(503, {"error": {"reason": self.state}})
        checked = validate(body, self.limits)
        if isinstance(checked, Reply):
            self.stats["rejected"] += 1
            return checked
        query, texts = checked
        t0 = time.perf_counter()
        with self._lock:  # ein Lauf zur Zeit: die iGPU teilt sich nicht gut, und so bleibt der Speicher begrenzt
            logits = self.engine.score(query, texts)
        self.stats["busy_ms"] += (time.perf_counter() - t0) * 1000
        if len(logits) != len(texts) or not all(math.isfinite(x) for x in logits):
            return error(500, "engine_output_invalid")
        self.stats["requests"] += 1
        self.stats["texts"] += len(texts)
        ranked = sorted(range(len(texts)), key=lambda i: -logits[i])
        return Reply(200, [{"index": i, "score": sigmoid(logits[i])} for i in ranked])
