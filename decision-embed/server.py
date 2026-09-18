#!/usr/bin/env python3
"""Stufe 2 als lokaler Dienst: Embedding (ONNX Runtime, CPU) + Klassifikationskopf, gleiches Protokoll wie decision-jevlike.

    python server.py --model-dir models/e5-small [--mode mix|logreg|knn] [--port 8082] [--threads 4]
    POST /decide {"context": "...", "options": [...]}  ->  {"probabilities": {...}, "model": ..., "encoder": ..., "latency_ms": ...}
    GET  /health
Zur Laufzeit nur numpy, onnxruntime, tokenizers - kein torch, kein transformers, kein Netz (Modell und Kopf liegen im Ordner).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from embedder import Embedder  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--model-dir", default=os.path.join(HERE, "models", "e5-small"))
ap.add_argument("--mode", default=None, choices=(None, "logreg", "knn", "mix"))
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8082)
ap.add_argument("--threads", type=int, default=None)
ap.add_argument("--fp32", action="store_true")
ARGS = ap.parse_args()

EMB = Embedder(ARGS.model_dir, quantized=not ARGS.fp32, threads=ARGS.threads)
HEAD = json.load(open(os.path.join(ARGS.model_dir, "head.json"), encoding="utf-8"))
MODE = ARGS.mode or HEAD.get("mode", "mix")
CLASSES = HEAD["classes"]
W = np.array(HEAD["logreg"]["weights"], dtype=np.float32)
B = np.array(HEAD["logreg"]["bias"], dtype=np.float32)
KV = np.array(HEAD["knn"]["vectors"], dtype=np.float32)
KL = np.array(HEAD["knn"]["labels"])
KK, KT = int(HEAD["knn"]["k"]), float(HEAD["knn"]["temperature"])
LOCK = threading.Lock()
STATS = {"decisions": 0, "errors": 0, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}


def softmax(z):
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def head_probs(vec: np.ndarray) -> np.ndarray:
    p_lr = softmax(W @ vec + B)
    if MODE == "logreg":
        return p_lr
    sims = KV @ vec
    idx = np.argsort(-sims)[:KK]
    wgt = np.exp((sims[idx] - sims[idx].max()) / KT)
    p_knn = np.zeros(len(CLASSES), dtype=np.float32)
    for j, wj in zip(idx, wgt, strict=True):
        p_knn[KL[j]] += wj
    p_knn /= p_knn.sum()
    return p_knn if MODE == "knn" else (p_lr + p_knn) / 2


def decide(context: str, options: list[str]) -> dict:
    with LOCK:
        vec = EMB.encode([context])[0]
    p = head_probs(vec)
    known = {c: float(p[i]) for i, c in enumerate(CLASSES)}
    return {o: known.get(o, 0.0) for o in options}


class Handler(BaseHTTPRequestHandler):
    server_version = "skirnir-embed/0.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True, "model": HEAD["name"], "encoder": EMB.name, "onnx": EMB.file, "mode": MODE,
                                    "classes": CLASSES, "n_train": HEAD.get("n_train"), "offline": True, **STATS})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/decide":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            context, options = str(body.get("context") or ""), [str(o) for o in (body.get("options") or [])]
            if not context or len(options) < 2:
                return self._send(400, {"error": "context und mindestens zwei options erwartet"})
            t0 = time.perf_counter()
            probs = decide(context, options)
            STATS["decisions"] += 1
            self._send(200, {"probabilities": probs, "model": HEAD["name"], "encoder": f"{EMB.name}/{MODE}",
                             "latency_ms": round((time.perf_counter() - t0) * 1000, 2)})
        except Exception as e:  # noqa: BLE001 - der Dienst darf an einer Anfrage nicht sterben
            STATS["errors"] += 1
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    print(f"skirnir-embed: {HEAD['name']} ({EMB.file}, mode {MODE}) auf http://{ARGS.host}:{ARGS.port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer((ARGS.host, ARGS.port), Handler).serve_forever()
