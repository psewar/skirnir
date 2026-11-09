# Training data of the decision models

How the synthetic data for the auto role and the privacy check is built, checked and evaluated, what it measured on
2026-11-09, and what limits it has. The reasons are in the ADRs:
[ADR-0077](../docs/adr/0077-decision-training-data-is-neutral-and-checked-against-its-builders.md) (neutral data, check),
[ADR-0078](../docs/adr/0078-privacy-threshold-from-out-of-fold-scores.md) (privacy threshold),
[ADR-0079](../docs/adr/0079-compare-decision-models-over-repeated-template-splits.md) (repeated splits).

## What is trained on what

| Data set | Builder | Rows | Models | Where the model lives |
|---|---|---|---|---|
| Roles (`standard`, `gross`, `assist`, `code`), German and English | `decision-eval/build_dataset.py` | 127 templates, 1216 sentences | tf-idf (`decision-eval/train_tfidf.py`), embedding head (`decision-embed/train_head.py`) | `router/decision-tfidf.json` in the repository; the head next to the e5 model where the service runs |
| Privacy (personal or neutral), German | `decision-embed/privacy/build_dataset.py` | 116 templates, 755 sentences; test: 100 hand-written | privacy head (`decision-embed/privacy/train_privacy.py`) | `privacy.json` next to the e5 model where the service runs |

## How it is built

* **Templates and slots.** Each template has placeholders; the builder fills them from slot lists with a fixed seed.
  Every slot value fits every template that uses it: German values carry their article, case and preposition
  ("in der Küche", "den Fernseher"), English templates take English values, a template that starts with a placeholder is
  capitalized after filling.
* **Language and names.** Standard German spelling, generic names (common first names, two common surnames), well-known
  cities and topics, no real place, institution, brand or household.
* **Surface variants** (roles only): greetings and thanks in the language of the sentence; "!" replaces a closing full
  stop, "bitte"/"please" goes before the closing mark, no polite prefix before a question or twice in one sentence.
* **Groups and split.** All sentences of a template share a group (a hash of role or label and template text) and land
  in the same part. Roles: per role 70 % train, 15 % validation, 15 % test groups. Privacy: a fifth of the templates per
  label in validation; the test set is written by hand and never generated.
* **Reproducible.** Both builders write the same bytes on every system. `test/check_training_data.py` (CI) rebuilds both
  data sets and compares them with the committed files, checks the spelling, and checks that the tf-idf model records
  the digest of the committed role data (`data_sha256`).

A change to a template or slot list is therefore one step: run the builder, retrain, commit data and model together.

```bash
cd decision-eval && python build_dataset.py && python train_tfidf.py            # roles: data and router/decision-tfidf.json
cd decision-embed/privacy && python build_dataset.py                             # privacy data
..\decision-jevlike\.venv\Scripts\python decision-embed\train_head.py --model-dir <e5 dir>           # embedding head
..\decision-jevlike\.venv\Scripts\python decision-embed\privacy\train_privacy.py --model-dir <e5 dir> # privacy head
python test/check_training_data.py
```

## How it is evaluated

* **One text at a time.** The e5 model is int8 with dynamic quantization: a vector depends on the batch it is computed
  in (cosine 0.992 to 0.996 against the single vector). The service embeds one request at a time, so training and
  evaluation do the same (`Embedder.encode_each`).
* **Repeated splits** (ADR-0079). `decision-eval/repeat_splits.py` and `decision-embed/privacy/repeat_splits.py` build,
  train and test with seeds 1 to 20 and report mean, spread and worst case. `--paired` keeps the old split for a
  builder that only changes wording.
* **Privacy threshold** (ADR-0078): the highest threshold that still finds 95 % of the personal sentences, chosen on
  out-of-fold scores over five template folds; the written head is trained on all templates.
* **`decision-eval/run_eval.py`** measures the engines through a router's `/admin/decide` on the committed split:
  top-1, top-3, confident-and-correct, fallback rate, latency, ECE, chains. It is one split (see above).

## Measurements of 2026-11-09

Local CPU, loopback router from this repository (`test/test-config.yaml` with the decision chain set to the engines
measured), no production system involved. The LLM engine was not re-measured (it is not trained on this data).

Over 50 template splits (`repeat_splits.py`), top-1 on the test part; the last column keeps the templates of the earlier
data in their splits and changes only the wording (20 splits, `--paired`, before the grammar fixes):

| Model | Earlier data | Neutral data | Same splits, wording only |
|---|---|---|---|
| tf-idf | 0.907 ± 0.061 (worst 0.669) | 0.907 ± 0.059 (worst 0.672) | 0.899 → 0.903 |
| embedding head | 0.955 ± 0.040 (worst 0.803) | 0.962 ± 0.033 (worst 0.838) | 0.949 → 0.956 |

The standard error of a 50-split mean is about 0.009 for tf-idf. With 20 splits it is about 0.017, and that is what a
one-word change does: it changes one group id, every split, and the 20-split mean (0.901 before replacing one word of
one template, 0.884 after; 0.907 for both over 50 splits).

Spelling, on the same splits: the old tf-idf model scores 0.900 on standard spelling (0.899 on its own), the new one
0.902 on ASCII spelling (0.903 on its own); the old embedding head 0.957 on standard spelling, the new one 0.948 on ASCII.
Neither model depends on how umlauts and ß are written.

Privacy head over 20 template splits (`decision-embed/privacy/repeat_splits.py`), recall and precision on the
hand-written test set. The head in use was trained with a recall target of 0.98, now the default:

| Target | Threshold procedure | Data | Threshold | Recall | Precision |
|---|---|---|---|---|---|
| 0.98 | validation fifth (before) | earlier | 0.55 ± 0.19 (0.27 to 0.87) | 0.898 ± 0.082 (worst 0.76) | 0.973 |
| 0.98 | validation fifth | neutral | 0.61 ± 0.15 | 0.900 ± 0.078 (worst 0.72) | 0.992 |
| 0.98 | out-of-fold | earlier | 0.46 ± 0.06 | 0.955 ± 0.017 (worst 0.94) | 0.976 |
| 0.98 | out-of-fold (now) | neutral | 0.47 ± 0.07 (0.23 to 0.54) | 0.964 ± 0.012 (worst 0.96) | 0.979 |
| 0.95 | validation fifth | earlier | 0.66 ± 0.18 | 0.851 ± 0.093 (worst 0.72) | 0.988 |
| 0.95 | validation fifth | neutral | 0.72 ± 0.15 | 0.838 ± 0.093 (worst 0.64) | 0.999 |
| 0.95 | out-of-fold | earlier | 0.59 ± 0.05 | 0.917 ± 0.028 (worst 0.86) | 0.999 |
| 0.95 | out-of-fold | neutral | 0.61 ± 0.07 | 0.918 ± 0.036 (worst 0.88) | 0.997 |

The procedure, not the data, makes the difference: with the same procedure, earlier and neutral data lie within each
other's spread.

Committed split and the heads as written (single split, for orientation only):

| | before | after |
|---|---|---|
| tf-idf test top-1 / confident and correct / fallback / ECE | 0.849 / 0.971 / 0.212 / 0.134 | 0.938 / 0.985 / 0.261 / 0.216 |
| embedding head | 0.932 / 0.967 / 0.083 / 0.119 | 0.972 / 1.000 / 0.051 / 0.084 |
| rules (not trained; moves with the split only) | 0.523 | 0.648 |
| chain `embed → tfidf → rules` | 0.909 | 0.977 |
| privacy head (target 0.98): threshold / test recall / precision | 0.40 / 1.00 / 0.98 | 0.30 / 1.00 / 0.94 |

The jump in the single-split numbers is mostly the split: the group ids are hashes of the template text, so every
reworded template moved to another part. On this split tf-idf is less sure of itself (more fallbacks to the next engine,
higher ECE); calibration temperatures stay at 0.5 for tf-idf and the embedding head. The privacy head before is the one
in use (trained 2026-10-23, reproduced with the code of that time); on the reworded test set it finds all 50 personal
requests and flags 2 neutral ones. The new head flags "Wer war Albert Einstein?", "Wie viel Uhr ist es in New York?" and
"Was ist die Formel für die Kreisfläche?"; its threshold comes from the fixed fold seed and lies on the cautious side of
the 20-seed range. At a target of 0.95 it misses "Was steht morgen in meinem Kalender?" and "Wann war im Kinderzimmer
zuletzt Bewegung?".

## What the data does not cover

Findings from the data itself (no model involved), 2026-11-09:

1. **One split says little.** 19 test templates per split; the five largest groups hold 12 % of all role sentences.
   Spread of ±0.05 to ±0.07 in top-1 and ±0.09 in privacy recall come from the split alone.
2. **Fewer sentences than rows.** 1216 role rows are 624 distinct sentences; the rest are greetings and thanks around
   them. 127 role templates and 116 privacy templates are the real size of the data.
3. **Length gives the role away.** Assist sentences are at most 64 characters, `gross` sentences at least 103;
   standard 21 to 179, code 35 to 135. The tf-idf model has a length feature. Real requests break this: a short request
   for a deep analysis, a long voice command with context, a pasted text with "summarize".
4. **Leakage between groups.** German and English versions of the same template are separate groups and can land in
   train and test; slot values (topics, persons, devices) are shared by all groups. In the privacy test set 21 of 100
   sentences contain a name or person from the builder's lists; 66 % of the test words occur in training.
5. **Balance.** Overall 267 to 362 rows per role, but per split uneven (train: `gross` 297, `standard` 182, `assist`
   181). Privacy is balanced (50/50 in the test set, about 50 % in training).
6. **Hard cases are missing.** No neutral privacy template talks about people (the test set asks "Wer war Albert
   Einstein?"); no personal template looks like a device query (motion history, a door open while nobody was home, a
   calendar entry, a sensitive item on a list). The remaining privacy misses are exactly these.
7. **Written, not spoken.** Templates are clean sentences with punctuation. Voice transcripts often have none, write
   numbers as words and contain fillers or recognition errors; chat has typos; real requests carry system prompts and
   tool context.
8. **Languages.** Roles: about 70 % German, 30 % English. Privacy: German only; an English request to the privacy
   classifier is not measured.
9. **Training stops at its limit.** tf-idf and both heads pick the last epoch (3000 or 4000) as the best: validation loss
   is still falling. The tf-idf calibration temperature sits at the lower end of the grid (0.5): its probabilities are
   too flat.

Labels: every template was read once during this change; no wrong label was found. The open questions are the
boundaries (when is a request `gross` rather than `standard`; is the state of a lock personal), not single labels.

## Proposals

In order of value for effort. None is done in this change; each needs the repeated-split numbers before and after.

| # | Proposal | Effort | Expected effect |
|---|---|---|---|
| 1 | Give translated and near-identical templates one group (an explicit family key next to the template) | small | honest test numbers for the embedding head; probably lower |
| 2 | Privacy: templates for the uncovered personal cases and for neutral questions about people, written from the definition in the builder; then a **new** hand-written test set, because the current one has now been looked at | medium | recall on device-like personal requests, fewer false alarms on public persons |
| 3 | Roles: short `gross` and long `standard`/`assist` templates, so length stops being the answer | small | robustness on real traffic; single-split numbers may drop |
| 4 | Hold out slot values per part (names, cities, devices), so the test measures unseen values | small | separates template from slot-value generalization |
| 5 | Voice-like variants: no punctuation, lower case, numbers as words, fillers | small | closer to speech-to-text input |
| 6 | English privacy templates and test sentences | medium | coverage for English clients |
| 7 | Evaluate on the router's anonymized capture with client labels (`build_dataset.py --capture`) on the router host only, publishing aggregate numbers, never sentences | medium | the first measurement on real traffic |
| 8 | Train to convergence (more epochs or a second-order optimizer) and re-check calibration | small | sharper tf-idf probabilities, fewer needless fallbacks |
| 9 | Privacy threshold from several fold seeds pooled (repeated k-fold): the fold seed alone moves it between 0.23 and 0.54 | small | a threshold that no longer depends on one fold seed |
