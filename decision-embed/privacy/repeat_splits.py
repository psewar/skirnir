#!/usr/bin/env python3
"""The privacy head over repeated template splits (ADR-0078, ADR-0079): how much do threshold and test recall depend
on which templates the builder puts where?

    ..\\..\\decision-jevlike\\.venv\\Scripts\\python repeat_splits.py BUILDER TEST.jsonl [TEST.jsonl ...]
        [--method oof|validation] [--seeds 20] [--model-dir ../models/e5-small] [--recall 0.98]

For seeds 1..N the builder's generator is reseeded (new fills and a new validation fifth), a head is trained and its
threshold chosen, and recall/precision on each hand-written test file are reported.
  oof        - as train_privacy.py: threshold from out-of-fold scores over five template folds, head on all templates
  validation - the earlier procedure: head on the train templates, threshold on the validation fifth
Embeddings one text at a time, as the service computes them.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import statistics
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from embedder import Embedder  # noqa: E402
from folds import highest_threshold, template_folds  # noqa: E402
from train_head import knn_probs, softmax, train_logreg  # noqa: E402


def build(builder, seed):
    """Rows like the builder's main(), with its generator reseeded."""
    builder.RNG = random.Random(seed)
    rows = []
    for label, templates in ((1, builder.PERSONAL), (0, builder.NEUTRAL)):
        order = list(range(len(templates)))
        builder.RNG.shuffle(order)
        val = set(order[: len(templates) // 5])
        for i, tpl in enumerate(templates):
            group = hashlib.sha1(f"{label}|{tpl}".encode()).hexdigest()[:10]
            for text in builder.fill(tpl, 12):
                rows.append({"text": text, "label": label, "group": group, "validation": i in val})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("builder")
    ap.add_argument("tests", nargs="+")
    ap.add_argument("--method", choices=("oof", "validation"), default="oof")
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--model-dir", default=os.path.join(HERE, "..", "models", "e5-small"))
    ap.add_argument("--recall", type=float, default=0.98)   # the value the head in use was trained with (ADR-0078)
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("builder", a.builder)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    tests = [[json.loads(x) for x in open(p, encoding="utf-8") if x.strip()] for p in a.tests]
    emb, cache = Embedder(a.model_dir, quantized=True, threads=4), {}

    def vecs(texts):
        for t in dict.fromkeys(t for t in texts if t not in cache):
            cache[t] = emb.encode([t])[0]
        return np.stack([cache[t] for t in texts])

    def fit(train, held):
        x, y = vecs([r["text"] for r in train]), np.array([r["label"] for r in train])
        _, w, b, _ = train_logreg(x, y, vecs([r["text"] for r in held]), np.array([r["label"] for r in held]), 2)
        return x, y, w, b

    def score(model, texts):
        x, y, w, b = model
        q = vecs(texts)
        return (softmax(q @ w.T + b)[:, 1] + knn_probs(q, x, y, 2)[:, 1]) / 2

    res = {}
    for seed in range(1, a.seeds + 1):
        rows = build(builder, seed)
        if a.method == "validation":
            tr, va = [r for r in rows if not r["validation"]], [r for r in rows if r["validation"]]
            model = fit(tr, va)
            th = highest_threshold(score(model, [r["text"] for r in va]).tolist(), [r["label"] for r in va], a.recall)
        else:
            fold_of = template_folds([r["group"] for r in rows], [r["label"] for r in rows], 5, seed)
            oof = np.zeros(len(rows))
            for f in range(5):
                held_idx = [i for i, r in enumerate(rows) if fold_of[r["group"]] == f]
                held = [rows[i] for i in held_idx]
                oof[held_idx] = score(fit([r for r in rows if fold_of[r["group"]] != f], held), [r["text"] for r in held])
            th = highest_threshold(oof.tolist(), [r["label"] for r in rows], a.recall)
            model = fit(rows, rows)
        res.setdefault("threshold", []).append(th)
        line = [f"seed {seed} threshold {th}"]
        for i, rows_t in enumerate(tests):
            y = np.array([r["label"] for r in rows_t])
            pred = score(model, [r["text"] for r in rows_t]) >= th
            tp, fp, fn = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum()), int((~pred & (y == 1)).sum())
            res.setdefault(f"recall[{i}]", []).append(tp / max(tp + fn, 1))
            res.setdefault(f"precision[{i}]", []).append(tp / max(tp + fp, 1))
            line.append(f"test[{i}] recall {tp / max(tp + fn, 1):.2f} precision {tp / max(tp + fp, 1):.2f}")
        print("  ".join(line), flush=True)
    for k, v in res.items():
        print(f"{k}: mean {statistics.fmean(v):.3f}  sd {statistics.pstdev(v):.3f}  min {min(v):.3f}  max {max(v):.3f}")


if __name__ == "__main__":
    main()
