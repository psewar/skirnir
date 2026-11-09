"""Self-test for decision-embed/privacy/folds.py (ADR-0078): template folds and the recall threshold of the privacy
head, including the fail-closed case. Standard library only."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "decision-embed", "privacy"))
import folds  # noqa: E402

failures = []


def expect(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  {detail}"))
    if not ok:
        failures.append(label)


# 12 personal and 18 neutral templates, three sentences each
groups = [f"p{i}" for i in range(12) for _ in range(3)] + [f"n{i}" for i in range(18) for _ in range(3)]
labels = [1] * 36 + [0] * 54
f = folds.template_folds(groups, labels, 5, 17)
expect("every template has exactly one fold", set(f) == set(groups) and all(0 <= v < 5 for v in f.values()), f)
expect("every fold holds personal and neutral templates",
       all({g[0] for g, v in f.items() if v == k} == {"p", "n"} for k in range(5)), f)
sizes = [sum(1 for g, v in f.items() if v == k and g[0] == "p") for k in range(5)]
expect("personal templates are spread evenly", max(sizes) - min(sizes) <= 1, sizes)
expect("same seed, same folds", folds.template_folds(groups, labels, 5, 17) == f)
expect("another seed deals differently", folds.template_folds(groups, labels, 5, 18) != f)

ht = folds.highest_threshold
scores, ys = [0.9, 0.8, 0.7, 0.2, 0.6, 0.1], [1, 1, 1, 1, 0, 0]
th = ht(scores, ys, 0.75)
expect("threshold reaches the recall target", folds.recall_at(scores, ys, th) >= 0.75, th)
expect("threshold is the highest that reaches it", th == 0.7, th)
expect("one step higher misses the target", folds.recall_at(scores, ys, round(th + 0.01, 2)) < 0.75)
expect("full recall goes down to the lowest personal score", ht(scores, ys, 1.0) == 0.2, ht(scores, ys, 1.0))
expect("unreachable recall falls back to the lowest threshold (fail closed)", ht([0.01, 0.9], [1, 1], 1.0) == 0.05,
       ht([0.01, 0.9], [1, 1], 1.0))
expect("no personal sentences: lowest threshold (fail closed)", ht([0.3], [0], 0.95) == 0.05, ht([0.3], [0], 0.95))

fp = folds.fold_problem
expect("enough templates per class are accepted", fp(groups, labels, 5) is None, fp(groups, labels, 5))
expect("too few personal templates are refused", fp(["p0", "p1", "n0", "n1", "n2"], [1, 1, 0, 0, 0], 3) is not None)
expect("no neutral templates are refused", fp(["p0", "p1", "p2"], [1, 1, 1], 3) is not None)

print("ALL OK" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
