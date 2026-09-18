#!/usr/bin/env python3
"""Stufe 1: TF-IDF (Zeichen-n-Gramme + Woerter) und Softmax-Regression fuer die Rollenwahl trainieren - numpy reicht.

    python train_tfidf.py [--out ../router/decision-tfidf.json] [--min-df 2] [--l2 1e-5] [--epochs 3000] [--lr 1.0]
Merkmale kommen aus router/ollama_router/decision/tfidf.py (dieselbe Funktion wie im Router). Modell = JSON mit Vokabular,
IDF, Gewichten, Bias, Klassen und Kennzahlen; der Router laedt es ueber decision_engine.tfidf.model_path.
Training: Vollbatch-Gradientenabstieg mit L2-Strafe, Wahl der Epoche mit der besten Validierungs-NLL (kein Ueberfitten
auf den Testsatz). Danach Top-1 auf Validierung und Test.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from ollama_router.decision.tfidf import features  # noqa: E402

DATA = os.path.join(HERE, "data")


def load(name):
    return [json.loads(l) for l in open(os.path.join(DATA, f"{name}.jsonl"), encoding="utf-8") if l.strip()]


def build_vocab(rows, min_df):
    df = Counter()
    for r in rows:
        df.update(set(features(r["context"])))
    vocab = {f: i for i, f in enumerate(sorted(f for f, c in df.items() if c >= min_df))}
    n = len(rows)
    idf = [math.log((1 + n) / (1 + df[f])) + 1.0 for f in vocab]
    return vocab, idf


def matrix(rows, vocab, idf):
    x = np.zeros((len(rows), len(vocab)), dtype=np.float32)
    for i, r in enumerate(rows):
        for f, c in features(r["context"]).items():
            j = vocab.get(f)
            if j is not None:
                x[i, j] = (1 + math.log(c)) * idf[j]
        norm = np.linalg.norm(x[i]) or 1.0
        x[i] /= norm
    return x


def softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def train(x, y, xv, yv, classes, l2, epochs, lr, seed=7):
    rng = np.random.default_rng(seed)
    k, d = len(classes), x.shape[1]
    w = rng.normal(0, 0.01, (k, d)).astype(np.float32)
    b = np.zeros(k, dtype=np.float32)
    onehot = np.eye(k, dtype=np.float32)[y]
    best = (float("inf"), None, None, 0)
    for ep in range(1, epochs + 1):
        p = softmax(x @ w.T + b)
        grad_w = (p - onehot).T @ x / len(x) + l2 * w
        grad_b = (p - onehot).mean(axis=0)
        w -= lr * grad_w
        b -= lr * grad_b
        if ep % 10 == 0 or ep == epochs:
            pv = softmax(xv @ w.T + b)
            nll = -np.log(np.clip(pv[np.arange(len(yv)), yv], 1e-9, 1)).mean()
            if nll < best[0]:
                best = (nll, w.copy(), b.copy(), ep)
    return best


def top1(x, y, w, b):
    return float((softmax(x @ w.T + b).argmax(axis=1) == y).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "router", "decision-tfidf.json"))
    ap.add_argument("--min-df", type=int, default=2)
    ap.add_argument("--l2", type=float, default=1e-5)
    ap.add_argument("--epochs", type=int, default=3000)   # Vollbatch-GD konvergiert langsam; 400 waren zu wenig (0,70 -> 0,85)
    ap.add_argument("--lr", type=float, default=1.0)
    ap.add_argument("--name", default="skirnir-tfidf-v1")
    a = ap.parse_args()
    tr, va, te = load("train"), load("validation"), load("test")
    classes = list(tr[0]["options"])
    vocab, idf = build_vocab(tr, a.min_df)
    x, xv, xt = matrix(tr, vocab, idf), matrix(va, vocab, idf), matrix(te, vocab, idf)
    y, yv, yt = (np.array([r["label"] for r in rows]) for rows in (tr, va, te))
    t0 = time.time()
    nll, w, b, ep = train(x, y, xv, yv, classes, a.l2, a.epochs, a.lr)
    acc_v, acc_t = top1(xv, yv, w, b), top1(xt, yt, w, b)
    print(f"Vokabular {len(vocab)}  beste Epoche {ep}  val NLL {nll:.4f}  val Top-1 {acc_v:.3f}  test Top-1 {acc_t:.3f}  ({time.time() - t0:.1f} s)")
    conf = np.zeros((len(classes), len(classes)), dtype=int)
    for yi, pi in zip(yt, softmax(xt @ w.T + b).argmax(axis=1), strict=True):
        conf[yi, pi] += 1
    print("Konfusion (Zeile wahr):", {classes[i]: {classes[j]: int(conf[i, j]) for j in range(len(classes))} for i in range(len(classes))})
    model = {"name": a.name, "classes": classes, "vocab": vocab, "idf": [round(v, 5) for v in idf],
             "weights": [[round(float(v), 5) for v in row] for row in w], "bias": [round(float(v), 5) for v in b],
             "features": "char 3-5 in-word + word uni/bigram + length bucket", "trained_at": time.strftime("%Y-%m-%d %H:%M"),
             "n_train": len(tr), "validation_top1": round(acc_v, 4), "test_top1": round(acc_t, 4), "l2": a.l2, "best_epoch": ep}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(model, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"-> {a.out} ({os.path.getsize(a.out) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
