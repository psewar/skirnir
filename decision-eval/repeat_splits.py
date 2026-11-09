#!/usr/bin/env python3
"""Compare role models over repeated template splits instead of one (ADR-0079).

One split of about 130 templates into train/validation/test moves top-1 by several points on its own: which templates
land in the test fifth is chance. This script builds the data set with seeds 1..N, 50 by default (each seed fills and
splits anew), trains on the train part, and reports mean, spread and worst case of top-1 on the test part.

    python repeat_splits.py tfidf BUILDER [--paired OLD_BUILDER] [--seeds 50]
    ..\\decision-jevlike\\.venv\\Scripts\\python repeat_splits.py embed BUILDER --model-dir ..\\decision-embed\\models\\e5-small

BUILDER is a build_dataset.py (this one, or an older version, e.g. `git show <rev>:decision-eval/build_dataset.py`).
--paired OLD_BUILDER: both builders must consume the random generator alike (same templates in the same order, same
slot-list lengths, only the wording changed); then row i of one is row i of the other, the OLD split is used for both,
and both models also score the other's test sentences. That isolates a wording change from the split.
tfidf uses train_tfidf.py and the router's own features; embed uses the e5 embeddings one text at a time (as the
service) and the softmax head of decision-embed/train_head.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def load_builder(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tfidf:
    def __init__(self):
        sys.path.insert(0, os.path.join(HERE, "..", "router"))
        sys.path.insert(0, HERE)
        import train_tfidf
        self.t = train_tfidf

    def fit(self, train, val):
        t = self.t
        vocab, idf = t.build_vocab(train, 2)
        x, xv = t.matrix(train, vocab, idf), t.matrix(val, vocab, idf)
        y, yv = np.array([r["label"] for r in train]), np.array([r["label"] for r in val])
        _, w, b, _ = t.train(x, y, xv, yv, list(train[0]["options"]), 1e-5, 3000, 1.0)
        return vocab, idf, w, b

    def top1(self, model, rows):
        vocab, idf, w, b = model
        return self.t.top1(self.t.matrix(rows, vocab, idf), np.array([r["label"] for r in rows]), w, b)


class Embed:
    def __init__(self, model_dir):
        sys.path.insert(0, os.path.join(HERE, "..", "decision-embed"))
        from embedder import Embedder
        from train_head import softmax, train_logreg
        self.emb, self.softmax, self.train_logreg, self.cache = Embedder(model_dir, quantized=True, threads=4), softmax, train_logreg, {}

    def vecs(self, rows):
        for t in dict.fromkeys(r["context"] for r in rows if r["context"] not in self.cache):
            self.cache[t] = self.emb.encode([t])[0]
        return np.stack([self.cache[r["context"]] for r in rows])

    def fit(self, train, val):
        _, w, b, _ = self.train_logreg(self.vecs(train), np.array([r["label"] for r in train]),
                                       self.vecs(val), np.array([r["label"] for r in val]), 4)
        return w, b

    def top1(self, model, rows):
        w, b = model
        pred = self.softmax(self.vecs(rows) @ w.T + b).argmax(axis=1)
        return float((pred == np.array([r["label"] for r in rows])).mean())


def describe(name, values):
    return (f"{name}: mean {statistics.fmean(values):.3f}  sd {statistics.pstdev(values):.3f}  "
            f"min {min(values):.3f}  max {max(values):.3f}  (n={len(values)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine", choices=("tfidf", "embed"))
    ap.add_argument("builder")
    ap.add_argument("--paired", default=None, help="older builder that corresponds row by row")
    ap.add_argument("--seeds", type=int, default=50)   # standard error of the mean about 0.01 for tf-idf (ADR-0079)
    ap.add_argument("--model-dir", default=os.path.join(HERE, "..", "decision-embed", "models", "e5-small"))
    a = ap.parse_args()
    engine = Tfidf() if a.engine == "tfidf" else Embed(a.model_dir)
    new = load_builder(a.builder, "builder_new")
    old = load_builder(a.paired, "builder_old") if a.paired else None
    res = {}

    def add(key, value):
        res.setdefault(key, []).append(value)

    for seed in range(1, a.seeds + 1):
        rn = new.build(seed)
        if old is None:
            p = new.split_by_group(rn, 0.15, 0.15, seed)
            m = engine.fit(p["train"], p["validation"])
            add("test", engine.top1(m, p["test"]))
            add("validation", engine.top1(m, p["validation"]))
        else:
            ro = old.build(seed)
            if len(ro) != len(rn) or any(x["role"] != y["role"] for x, y in zip(ro, rn, strict=True)):
                sys.exit("repeat_splits: the builders do not correspond row by row; compare them without --paired")
            for x, y in zip(ro, rn, strict=True):
                y["group"] = x["group"]   # the old split for both
            po, pn = old.split_by_group(ro, 0.15, 0.15, seed), new.split_by_group(rn, 0.15, 0.15, seed)
            mo, mn = engine.fit(po["train"], po["validation"]), engine.fit(pn["train"], pn["validation"])
            add("old_on_old", engine.top1(mo, po["test"]))
            add("new_on_new", engine.top1(mn, pn["test"]))
            add("old_on_new_wording", engine.top1(mo, pn["test"]))
            add("new_on_old_wording", engine.top1(mn, po["test"]))
        print(seed, {k: round(v[-1], 3) for k, v in res.items()}, flush=True)
    for k, v in res.items():
        print(describe(k, v))
    print(json.dumps({k: [round(x, 4) for x in v] for k, v in res.items()}))


if __name__ == "__main__":
    main()
