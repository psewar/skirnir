#!/usr/bin/env python3
"""Train the privacy head on the same embeddings as the role head (train_head.py): personal (1) or neutral (0).

    ..\\..\\decision-jevlike\\.venv\\Scripts\\python train_privacy.py [--model-dir ../models/e5-small] [--recall 0.98]
Logistic regression and kNN on the e5 vectors, mixed. The threshold is the highest one that still finds `--recall` of
the personal requests: a missed personal request goes to the cloud, a false alarm only costs a cloud answer. It is chosen
on out-of-fold scores (ADR-0078): the generated templates (train and validation together) are dealt into five folds by
template, each fold is scored by a head trained on the other four, and the threshold comes from all these scores. The
head that is written is then trained on all templates. Over 20 template splits a threshold from the single validation fifth
varied with sd 0.19, out-of-fold with sd 0.07 (recall 0.98). Reported on the hand-written test set at that threshold.
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
sys.path.insert(0, HERE)
from embedder import Embedder  # noqa: E402
from folds import fold_problem, highest_threshold, template_folds  # noqa: E402
from train_head import knn_probs, train_logreg  # noqa: E402

FOLDS, FOLD_SEED = 5, 17


def load(name):
    with open(os.path.join(HERE, "data", f"{name}.jsonl"), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def encode(emb, rows):
    return emb.encode_each([r["text"] for r in rows])


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
    ap.add_argument("--recall", type=float, default=0.98)   # the value the head in use was trained with (ADR-0078)
    a = ap.parse_args()
    rows, te = load("train") + load("validation"), load("test")   # every generated template; test is hand-written
    problem = fold_problem([r.get("group", "") for r in rows], [r["label"] for r in rows], FOLDS)
    if any("group" not in r for r in rows) or problem:
        sys.exit(f"train_privacy: cannot choose the threshold out-of-fold: {problem or 'rows without group'}. "
                 "Regenerate the data with build_dataset.py (it writes each row's template group).")
    emb = Embedder(a.model_dir, quantized=True, threads=a.threads)
    t0 = time.time()
    x, xte = encode(emb, rows), encode(emb, te)
    y, yte = np.array([r["label"] for r in rows]), np.array([r["label"] for r in te])
    print(f"Embeddings: {len(rows)}/{len(te)} in {time.time() - t0:.1f} s")

    def probs(q, w, b, xk, yk):
        return (softmax(q @ w.T + b)[:, 1] + knn_probs(q, xk, yk, 2)[:, 1]) / 2

    fold_of = template_folds([r["group"] for r in rows], [r["label"] for r in rows], FOLDS, FOLD_SEED)
    fold = np.array([fold_of[r["group"]] for r in rows])
    oof = np.zeros(len(rows))
    for f in range(FOLDS):   # each template is scored by a head that never saw it
        tr, ho = fold != f, fold == f
        _, w, b, _ = train_logreg(x[tr], y[tr], x[ho], y[ho], 2)
        oof[ho] = probs(x[ho], w, b, x[tr], y[tr])
    th = highest_threshold(oof.tolist(), y.tolist(), a.recall)
    _, w, b, ep = train_logreg(x, y, x, y, 2)   # the head that is written: all templates
    pte = probs(xte, w, b, x, y)
    res = {"threshold": th, "threshold_method": f"out-of-fold, {FOLDS} template folds", "out_of_fold": metrics(oof, y, th),
           "test": metrics(pte, yte, th), "test_at_0.5": metrics(pte, yte, 0.5)}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    miss = [r["text"] for r, p in zip(te, pte, strict=True) if r["label"] == 1 and p < th]
    alarm = [r["text"] for r, p in zip(te, pte, strict=True) if r["label"] == 0 and p >= th]
    print("Test, personal not found:", miss)
    print("Test, neutral flagged:", alarm)
    out = os.path.join(a.model_dir, "privacy.json")
    head = {"name": "skirnir-privacy-e5-small-v2", "encoder": emb.name, "classes": ["neutral", "personal"],
            "logreg": {"weights": w.round(6).tolist(), "bias": b.round(6).tolist()},
            "knn": {"k": 7, "temperature": 0.05, "vectors": x.round(5).tolist(), "labels": y.tolist()},
            "threshold": th, "recall_target": a.recall, "results": res, "best_epoch": ep,
            "trained_at": time.strftime("%Y-%m-%d %H:%M"), "n_train": len(rows)}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(head, f)
    print(f"-> {out} ({os.path.getsize(out) / 2**20:.1f} MB), threshold {th}")


if __name__ == "__main__":
    main()
