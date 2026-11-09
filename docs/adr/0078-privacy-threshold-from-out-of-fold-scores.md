---
status: proposed
date: 2026-11-09
decision-makers: operator
---

# The privacy classifier's threshold comes from out-of-fold scores over all templates

> Source: measurement while retraining the privacy head on the neutral data
> ([ADR-0077](0077-decision-training-data-is-neutral-and-checked-against-its-builders.md)), 2026-11-09.

## Context and Problem Statement

The content check before a cloud tier ([ADR-0038](0038-content-check-before-a-cloud-tier-opt-in.md)) keeps a request
local when the privacy classifier's probability reaches its threshold. `decision-embed/privacy/train_privacy.py` chose
that threshold as the highest one that still finds the target share of personal sentences (`--recall`; the head in use
was trained with 0.98) in the validation set, which is one fifth of the templates. Which templates land in that fifth is
chance. Over 20 template splits of the same data, at a target of 0.98, the threshold ranged from 0.27 to 0.87 (sd 0.19)
and recall on the hand-written test set from 0.76 to 1.00 (mean 0.90). The head in use (recall 1.00 on its test set) came
from a favourable split. A missed personal request may leave the house; the threshold must not depend on such chance.
The script's default target was 0.95, not the 0.98 the head in use was trained with.

## Decision Drivers

* Recall of personal requests is the safety property; false alarms only cost a local answer.
* The threshold must be stable under the random choice of templates, not tuned on the test set.
* No new dependencies; the service and the head format stay the same.
* The training script does what is meant without a flag that has to be remembered.

## Considered Options

* Out-of-fold threshold: five folds by template over all generated templates; every template is scored by a head that
  never saw it; the threshold comes from all these scores; the head that is written is trained on all templates
* Keep the validation fifth, but average the threshold over several splits
* A fixed threshold (for example 0.5)
* Keep the procedure as it is

## Decision Outcome

Chosen option: "out-of-fold threshold", because it uses every template for the threshold and every template for the
head, and it is the usual way to choose a cut-off without a separate hold-out. Folds are dealt per class from a fixed
seed, so the result is reproducible; a data set with fewer templates per class than folds is refused before training;
without evidence (no personal sentences) the lowest threshold is chosen, so more requests stay local (fail closed). The
default recall target becomes 0.98, the value the head in use was trained with.

### Consequences

* Good, because over the same 20 splits at a target of 0.98 test recall rises from 0.898 ± 0.082 (worst split 0.76) to
  0.964 ± 0.012 (worst 0.96) at the same precision (0.973 before, 0.979 now), and the threshold spread falls from sd 0.19
  to 0.07. At a target of 0.95: recall 0.851 ± 0.093 before, 0.918 ± 0.036 now. The data change alone
  ([ADR-0077](0077-decision-training-data-is-neutral-and-checked-against-its-builders.md)) moves none of these beyond
  the spread (old data out-of-fold at 0.98: 0.955 ± 0.017).
* Good, because the written head is trained on all templates (755 sentences instead of 594).
* Neutral, because the head written now (fixed fold seed, target 0.98) has threshold 0.30 and finds all 50 personal test
  requests, and flags 3 of 50 neutral ones (precision 0.94); the head in use had 0.40, 50 of 50 and 1 flagged. The fold
  seed moves the threshold too (0.23 to 0.54 over 20 seeds); 0.30 is on the cautious side.
* Bad, because training fits six heads instead of one (seconds on a CPU), and the out-of-fold numbers replace the
  validation numbers in `privacy.json`.
* Bad, because the measured gain rests on 100 hand-written test sentences; the remaining misses at lower targets are
  content the templates do not cover (a calendar entry, motion in a child's room), see
  [design/decision-training-data.md](../../design/decision-training-data.md).

### Confirmation

`test/selftest_privacy_threshold.py` in CI: every template in one fold, both classes in each fold, the threshold is the
highest that reaches the recall, unreachable recall and missing evidence give the lowest threshold, too few templates
are refused. The written head records `threshold_method` and the out-of-fold metrics.
`decision-embed/privacy/repeat_splits.py` reproduces the numbers above (`--method validation|oof`, `--recall`).
