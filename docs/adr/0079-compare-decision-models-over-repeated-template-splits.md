---
status: proposed
date: 2026-11-09
decision-makers: operator
---

# Compare decision models over repeated template splits

> Source: measurements while retraining on the neutral data
> ([ADR-0077](0077-decision-training-data-is-neutral-and-checked-against-its-builders.md)), 2026-11-09.

## Context and Problem Statement

The role data set has 127 templates; the builder puts all sentences of a template into one of train, validation and test,
so the test part holds 19 templates. Which ones is chance, decided by the seed and by the template text (the group id is
its hash). Over 20 seeds the tf-idf model's test top-1 ranged from 0.67 to 0.98 on the earlier data; the privacy
head's test recall from 0.72 to 0.98 ([ADR-0078](0078-privacy-threshold-from-out-of-fold-scores.md)). Changing the
wording of the templates moved the committed model's single-split number from 0.848 to 0.938 although, measured on the
same templates, nothing had changed. A single split cannot show whether a change helps, and documentation that quotes
one number suggests a precision it does not have.

## Decision Drivers

* A change to templates, builders or training must show its effect apart from the split.
* The comparison has to be cheap enough to run for every such change (minutes on a CPU).
* No model selection on the test part.

## Considered Options

* Repeated splits: build, train and test with seeds 1 to 20; report mean, spread and worst case; when only the wording
  changed, pair old and new on the same split
* k-fold cross-validation over templates with one seed
* Keep one split and accept the noise

## Decision Outcome

Chosen option: "repeated splits", because it reuses the builders' own split per role unchanged, needs no new split
logic, and the paired form isolates exactly what a wording change does.

* `decision-eval/repeat_splits.py` (tf-idf, embedding head; `--paired` for corresponding builders; 50 seeds by default)
  and `decision-embed/privacy/repeat_splits.py` (privacy head, both threshold procedures; 20 seeds) report mean,
  standard deviation, minimum and maximum.
* Because the group ids are hashes of the template text, any reworded template gives other splits for every seed. The
  mean over the seeds then still moves by its standard error (sd divided by the square root of the number of seeds):
  with 20 seeds a one-word change in one template moved the tf-idf mean from 0.901 to 0.884. A difference smaller
  than about two standard errors is reported as no measurable change; a wording-only change is measured `--paired`.
* A pull request that changes templates, builders or training quotes these numbers before and after; single-split
  numbers (the committed model, `run_eval.py`) are named as such.
* Documentation quotes the mean and spread, with the date and the data set.

### Consequences

* Good, because an effect smaller than the split noise is visible as such, and a worst case is reported.
* Good, because the paired comparison separated "same quality" (tf-idf 0.899 to 0.903) from "different split" (0.848 to
  0.938) in the change that introduced it.
* Bad, because a comparison takes minutes instead of seconds (about 10 for tf-idf over 50 seeds on one core, 25 paired
  over 20, a few for the embedding head).
* Bad, because templates are still not independent: translations of a template land in other groups, slot values are
  shared between groups; repeated splits average the noise, not this leakage
  ([design/decision-training-data.md](../../design/decision-training-data.md) proposes grouping them).

### Confirmation

The pull request description of a change to the decision data or training contains the repeated-split numbers; the
reviewer checks them. There is no automatic check: the measurement needs the embedding model, which CI does not have.
