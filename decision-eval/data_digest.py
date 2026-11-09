"""Digest of a decision-eval data set, recorded in the tf-idf model (train_tfidf.py) and compared by
test/check_training_data.py: a model whose digest differs from the committed data was trained on other data.
Standard library only."""

from __future__ import annotations

import hashlib
import os

FILES = ("train.jsonl", "validation.jsonl", "test.jsonl")


def digest(data_dir: str) -> str:
    """sha256 over the bytes of train, validation and test, in this order."""
    h = hashlib.sha256()
    for name in FILES:
        with open(os.path.join(data_dir, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()
