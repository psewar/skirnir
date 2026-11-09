"""Template folds and the recall threshold of the privacy head (train_privacy.py, ADR-0078). Standard library only, so
test/selftest_privacy_threshold.py runs without numpy or the embedding model.

The threshold decides which requests count as personal and stay local. It is chosen on out-of-fold scores: every
template is scored once by a head that never saw it, so all templates take part and no single validation fifth decides.
"""
from __future__ import annotations

import random

GRID = [round(0.05 + 0.01 * i, 2) for i in range(91)]   # 0.05 .. 0.95


def template_folds(groups: list[str], labels: list[int], k: int, seed: int) -> dict[str, int]:
    """Fold per template group, dealt round-robin per label after a seeded shuffle, so each fold holds personal and
    neutral templates and every sentence of a template is in the same fold."""
    rng = random.Random(seed)
    label_of = {}
    for g, y in zip(groups, labels, strict=True):
        label_of.setdefault(g, y)
    folds = {}
    for label in sorted(set(label_of.values())):
        members = sorted(g for g, y in label_of.items() if y == label)
        rng.shuffle(members)
        for i, g in enumerate(members):
            folds[g] = i % k
    return folds


def fold_problem(groups: list[str], labels: list[int], k: int) -> str | None:
    """Why the templates cannot be dealt into k folds with both classes in each, or None when they can."""
    label_of = {}
    for g, y in zip(groups, labels, strict=True):
        label_of.setdefault(g, y)
    for label, name in ((1, "personal"), (0, "neutral")):
        n = sum(1 for y in label_of.values() if y == label)
        if n < k:
            return f"{n} {name} templates, at least {k} are needed for {k} folds"
    return None


def recall_at(scores: list[float], labels: list[int], threshold: float) -> float:
    """Share of the personal sentences scoring at or above the threshold; 0 without personal sentences (no evidence)."""
    personal = [s for s, y in zip(scores, labels, strict=True) if y == 1]
    return sum(s >= threshold for s in personal) / len(personal) if personal else 0.0


def highest_threshold(scores: list[float], labels: list[int], recall: float) -> float:
    """Highest threshold on the grid at which at least `recall` of the personal sentences score at or above it. When no
    grid value reaches it, the lowest one: more requests count as personal and stay local (fail closed)."""
    ok = [t for t in GRID if recall_at(scores, labels, t) >= recall]
    return max(ok) if ok else GRID[0]
