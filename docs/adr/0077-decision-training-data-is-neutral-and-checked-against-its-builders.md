---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Decision training data is neutral, grammatical and checked against its builders

> Source: the operator's review of the public repository for details that point at a place (2026-11-08). Router 0.18.3
> cleaned the template scripts then, but left the generated data and the trained router model as they were.

## Context and Problem Statement

The auto role ([ADR-0017](0017-auto-role-through-a-chain-of-local-decision-engines.md)) and the content check before a
cloud tier ([ADR-0038](0038-content-check-before-a-cloud-tier-opt-in.md)) use models trained on synthetic sentences
built from templates: `decision-eval/build_dataset.py` for the roles (the tf-idf model `router/decision-tfidf.json` and
the embedding head of `decision-embed`) and `decision-embed/privacy/build_dataset.py` for the privacy head. Templates and
data did not follow the standard German spelling (umlauts written as ae/oe/ue, ss for ß) and named real places and
institutions. Router 0.18.3 changed the scripts, but the committed data and the model still held the old sentences,
because nothing tied the data to its builder. Many filled sentences were also wrong: an article that did not fit the
slot value ("das Deckenlampe", "im Küche"), points in time in duration slots ("Wecke mich 10 Minuten"), English templates
filled with German words ("How many calories are in CSV-Datei?"), greetings in the other language or before an
imperative that does not take one ("Kannst du schalte ...").

## Decision Drivers

* Nothing in the repository may point at a place or a household (AGENTS.md, "Public data").
* Templates, data and trained model must not drift apart again: a check in CI, not a reminder.
* No loss in routing or privacy quality, shown over more than one split of the templates.
* Real requests arrive with umlauts and ß (speech-to-text) and sometimes in ASCII; both have to work.

## Considered Options

* Clean the templates, regenerate the data, retrain the models, and let CI check data and model against the builders
* Search and replace in the generated files only
* Clean the templates only (what router 0.18.3 did)
* Fold spelling variants in the tf-idf features (ß to ss, ä to ae), so either spelling gives the same feature

## Decision Outcome

Chosen option: "regenerate, retrain and check", because only the builders make the data reproducible, and only a check
keeps them in step.

* Templates and the hand-written privacy test set use the standard German spelling and generic vocabulary, names,
  places and units; nothing names a real place, institution or household. The role name `gross` stays: it is an ASCII
  configuration key, not text.
* Every slot value fits every template that uses it (article, case, gender, preposition); English templates take English
  values; surface variants (greetings, thanks) follow the language of the sentence and fit its form.
* The builders write the same bytes on every system. `test/check_training_data.py` rebuilds both data sets in CI and
  compares them with the committed files, checks the spelling of templates, data, test set and model vocabulary, and
  checks that `router/decision-tfidf.json` records the digest of the data it was trained on (`data_sha256`,
  `decision-eval/data_digest.py`).
* Training and evaluation embed one text at a time, as the service does (`Embedder.encode_each`): the int8 model
  quantizes its activations per batch, so a vector computed in a batch differs from the service's (cosine 0.992 to
  0.996 on the privacy test set).
* Folding spelling variants is not done: on 20 paired splits the old model scored 0.900 on standard spelling against
  0.899 on its own, the new model 0.902 on ASCII spelling against 0.903 on its own. There is nothing to gain.

### Consequences

* Good, because templates, data and the router model follow the standard spelling and name nothing real.
* Good, because a template changed without regenerated data, or data regenerated without a retrained model, fails CI.
* Good, because quality held: tf-idf top-1 over 50 template splits 0.907 ± 0.061 before, 0.907 ± 0.059 after; embedding
  head 0.955 ± 0.040 before, 0.962 ± 0.033 after. With the same templates in the same splits and only the wording changed
  (20 splits): tf-idf 0.899 to 0.903, embedding head 0.949 to 0.956.
* Bad, because the group ids are hashes of the template text, so the split moved: the committed model's own test top-1
  went from 0.848 to 0.938 through the split alone. Single-split numbers before and after are not comparable;
  [ADR-0079](0079-compare-decision-models-over-repeated-template-splits.md) proposes how to compare.
* Bad, because CI runs both builders (about a second).
* Neutral, because retrained models reach a router only through a release that the operator deploys; the embedding head
  and the privacy head live outside the repository and are retrained where the service runs (`decision-embed/README.md`).

### Confirmation

`test/check_training_data.py` and `test/selftest_training_data.py` in CI (the self-test covers the rejected cases: ss
where the standard spelling has ß, stale data, a missing file, a model without or with another data digest). Added lines
are also read against the operator's private list of site terms before publishing.

## More Information

How the data is built and evaluated, the measurements and the open proposals:
[design/decision-training-data.md](../../design/decision-training-data.md).
