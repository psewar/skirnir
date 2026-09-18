#!/usr/bin/env python3
"""Stufe 2, Schritt 2: Klassifikationskopf auf den Embeddings trainieren und messen.

    ..\\decision-jevlike\\.venv\\Scripts\\python train_head.py [--model-dir models/e5-small] [--fp32] [--threads 4]
Zwei Koepfe auf denselben Vektoren: Softmax-Regression (numpy, wie tfidf) und kNN (Kosinus, k=7, abstandsgewichtet). Beide
werden auf Validierung/Test gemessen; head.json enthaelt beide plus den Trainingsvektoren fuer kNN, der Dienst waehlt per
"mode" (logreg | knn | mix). Dazu die Kosten einer Einbettung auf CPU (Median ueber den Testsatz, einzeln, N Threads) -
das ist die Zahl, die fuer den 4-Kern-CT zaehlt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from embedder import Embedder  # noqa: E402

DATA = os.path.join(HERE, "..", "decision-eval", "data")


def load(name):
    return [json.loads(l) for l in open(os.path.join(DATA, f"{name}.jsonl"), encoding="utf-8") if l.strip()]


def softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def train_logreg(x, y, xv, yv, k, l2=1e-5, epochs=4000, lr=1.0, seed=7):
    rng = np.random.default_rng(seed)
    w = rng.normal(0, 0.01, (k, x.shape[1])).astype(np.float32)
    b = np.zeros(k, dtype=np.float32)
    onehot = np.eye(k, dtype=np.float32)[y]
    best = (float("inf"), None, None, 0)
    for ep in range(1, epochs + 1):
        p = softmax(x @ w.T + b)
        w -= lr * ((p - onehot).T @ x / len(x) + l2 * w)
        b -= lr * (p - onehot).mean(axis=0)
        if ep % 10 == 0:
            pv = softmax(xv @ w.T + b)
            nll = -np.log(np.clip(pv[np.arange(len(yv)), yv], 1e-9, 1)).mean()
            if nll < best[0]:
                best = (nll, w.copy(), b.copy(), ep)
    return best


def knn_probs(q, xtr, ytr, k, kk=7, temp=0.05):
    """Kosinus-Aehnlichkeit zu allen Trainingsvektoren; die kk naechsten stimmen gewichtet (Softmax der Aehnlichkeit) ab."""
    sims = q @ xtr.T
    idx = np.argsort(-sims, axis=1)[:, :kk]
    out = np.zeros((len(q), k), dtype=np.float32)
    for i in range(len(q)):
        s = sims[i, idx[i]]
        wgt = np.exp((s - s.max()) / temp)
        for j, wj in zip(idx[i], wgt, strict=True):
            out[i, ytr[j]] += wj
    return out / out.sum(axis=1, keepdims=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=os.path.join(HERE, "models", "e5-small"))
    ap.add_argument("--fp32", action="store_true", help="model.onnx statt model_quantized.onnx")
    ap.add_argument("--threads", type=int, default=4, help="CPU-Threads wie auf dem Router-CT")
    ap.add_argument("--out", default=None)
    ap.add_argument("--epochs", type=int, default=4000)
    ap.add_argument("--lr", type=float, default=1.0)
    ap.add_argument("--l2", type=float, default=1e-5)
    a = ap.parse_args()
    emb = Embedder(a.model_dir, quantized=not a.fp32, threads=a.threads)
    tr, va, te = load("train"), load("validation"), load("test")
    classes = list(tr[0]["options"])
    t0 = time.time()
    xtr, xva, xte = (emb.encode([r["context"] for r in rows]) if False else np.concatenate([emb.encode([r["context"] for r in rows[i:i + 32]]) for i in range(0, len(rows), 32)]) for rows in (tr, va, te))
    print(f"Embeddings {emb.name} ({emb.file}, dim {xtr.shape[1]}): {len(tr) + len(va) + len(te)} Texte in {time.time() - t0:.1f} s")
    ytr, yva, yte = (np.array([r["label"] for r in rows]) for rows in (tr, va, te))
    k = len(classes)
    nll, w, b, ep = train_logreg(xtr, ytr, xva, yva, k, l2=a.l2, epochs=a.epochs, lr=a.lr)
    p_lr = softmax(xte @ w.T + b)
    p_knn = knn_probs(xte, xtr, ytr, k)
    p_mix = (p_lr + p_knn) / 2
    res = {}
    for name, p in (("logreg", p_lr), ("knn", p_knn), ("mix", p_mix)):
        pred = p.argmax(axis=1)
        conf = {classes[i]: {classes[j]: int(((yte == i) & (pred == j)).sum()) for j in range(k)} for i in range(k)}
        res[name] = {"test_top1": round(float((pred == yte).mean()), 4), "confusion": conf}
        print(f"{name:7s} test Top-1 {res[name]['test_top1']:.3f}")
    pv = softmax(xva @ w.T + b).argmax(axis=1)
    print(f"logreg val Top-1 {float((pv == yva).mean()):.3f} (beste Epoche {ep}, NLL {nll:.3f})")
    # Latenz einer Einbettung, einzeln, wie im Dienst
    lat = []
    for r in te[:60]:
        t1 = time.perf_counter()
        emb.encode([r["context"]])
        lat.append((time.perf_counter() - t1) * 1000)
    lat.sort()
    lat_info = {"threads": a.threads, "p50_ms": round(lat[len(lat) // 2], 1), "p95_ms": round(lat[int(len(lat) * 0.95)], 1), "file": emb.file}
    print("Einbettung einzeln:", lat_info)
    out = a.out or os.path.join(a.model_dir, "head.json")
    head = {"name": f"skirnir-embed-{os.path.basename(a.model_dir)}-v1", "encoder": emb.name, "onnx": emb.file, "prefix": emb.prefix,
            "classes": classes, "logreg": {"weights": w.round(6).tolist(), "bias": b.round(6).tolist()},
            "knn": {"k": 7, "temperature": 0.05, "vectors": xtr.round(5).tolist(), "labels": ytr.tolist()},
            "mode": max(res, key=lambda n: res[n]["test_top1"]), "results": res, "latency": lat_info,
            "trained_at": time.strftime("%Y-%m-%d %H:%M"), "n_train": len(tr)}
    json.dump(head, open(out, "w", encoding="utf-8"))
    print(f"-> {out} ({os.path.getsize(out) / 2**20:.1f} MB), mode={head['mode']}")


if __name__ == "__main__":
    main()
