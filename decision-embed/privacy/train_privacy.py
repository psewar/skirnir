#!/usr/bin/env python3
"""Train the privacy head on the same embeddings as the role head (train_head.py): personal (1) or neutral (0).

    ..\\..\\decision-jevlike\\.venv\\Scripts\\python train_privacy.py [--model-dir ../models/e5-small] [--recall 0.95]
Logistic regression and kNN on the e5 vectors, mixed. The threshold is chosen on the validation set (templates never
seen in training) as the highest one that still finds `--recall` of the personal requests: a missed personal request
goes to the cloud, a false alarm only costs a cloud answer. Reported on the hand-written test set at that threshold.
Writes privacy.json next to head.json; the service (server.py) answers POST /privacy with it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from embedder import Embedder  # noqa: E402
from train_head import knn_probs, train_logreg  # noqa: E402


def load(name):
    with open(os.path.join(HERE, "data", f"{name}.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def encode(emb, rows):
    return np.concatenate([emb.encode([r["text"] for r in rows[i:i + 32]]) for i in range(0, len(rows), 32)])


def softmax(z):
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def metrics(p, y, th):
    pred = p >= th
    tp, fp = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum())
    fn, tn = int((~pred & (y == 1)).sum()), int((~pred & (y == 0)).sum())
    return {"recall": round(tp / max(tp + fn, 1), 3), "precision": round(tp / max(tp + fp, 1), 3),
            "false_negative": fn, "false_positive": fp, "tp": tp, "tn": tn}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=os.path.join(HERE, "..", "models", "e5-small"))
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--recall", type=float, default=0.95)
    a = ap.parse_args()
    emb = Embedder(a.model_dir, quantized=True, threads=a.threads)
    tr, va, te = load("train"), load("validation"), load("test")
    t0 = time.time()
    xtr, xva, xte = encode(emb, tr), encode(emb, va), encode(emb, te)
    ytr, yva, yte = (np.array([r["label"] for r in rows]) for rows in (tr, va, te))
    print(f"Embeddings: {len(tr)}/{len(va)}/{len(te)} in {time.time() - t0:.1f} s")
    _, w, b, ep = train_logreg(xtr, ytr, xva, yva, 2)

    def probs(x):
        return (softmax(x @ w.T + b)[:, 1] + knn_probs(x, xtr, ytr, 2)[:, 1]) / 2
    pva, pte = probs(xva), probs(xte)
    # highest threshold that still reaches the recall on validation
    th = max((t for t in np.arange(0.05, 0.96, 0.01) if metrics(pva, yva, t)["recall"] >= a.recall), default=0.05)
    th = round(float(th), 2)
    res = {"threshold": th, "validation": metrics(pva, yva, th), "test": metrics(pte, yte, th),
           "test_at_0.5": metrics(pte, yte, 0.5)}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    miss = [r["text"] for r, p in zip(te, pte, strict=True) if r["label"] == 1 and p < th]
    alarm = [r["text"] for r, p in zip(te, pte, strict=True) if r["label"] == 0 and p >= th]
    print("Test, personal not found:", miss)
    print("Test, neutral flagged:", alarm)
    out = os.path.join(a.model_dir, "privacy.json")
    head = {"name": "skirnir-privacy-e5-small-v1", "encoder": emb.name, "classes": ["neutral", "personal"],
            "logreg": {"weights": w.round(6).tolist(), "bias": b.round(6).tolist()},
            "knn": {"k": 7, "temperature": 0.05, "vectors": xtr.round(5).tolist(), "labels": ytr.tolist()},
            "threshold": th, "recall_target": a.recall, "results": res, "best_epoch": ep,
            "trained_at": time.strftime("%Y-%m-%d %H:%M"), "n_train": len(tr)}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(head, f)
    print(f"-> {out} ({os.path.getsize(out) / 2**20:.1f} MB), threshold {th}")


if __name__ == "__main__":
    main()
