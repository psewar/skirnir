---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# Language-dependent defaults live in the configuration

> Source: the portability review of 2026-11-22 (making the router usable for other setups), router 0.37.0. Related:
> [ADR-0026](0026-english-names-for-interfaces-and-code.md) (English names for interfaces and code).

## Context and Problem Statement

Two values in the router core assumed the original installation:

- The warm benchmark of the model catalogue (`perf.bench_model`) sent a fixed German prompt that ends with "answer in
  German". Generation speed in tokens per second depends on the language of prompt and answer, because tokenizers split
  languages into differently many tokens. For an operator whose clients use another language the benchmark measures a
  case that does not occur, and the hard-coded text could not be changed.
- Observation mode (`decision_engine.observe_blocked`) records `would_fallback` only for the classes in
  `decision_engine.observe_light`. When that key was absent or an empty list, `fallback_observation.py` fell back to a
  constant `("standard", "assist")`: role names of one installation, hidden in a module, and an explicit `[]` could not
  mean "none".

A few status and log texts in core modules were German (tunnel and node-request errors, update status messages, the
fallback name of an agent without a usable name, two hints of the control port).

## Decision Drivers

* Existing measurements and existing configurations keep their meaning: defaults stay what they were.
* A default that depends on language or on role names is visible and changeable in the configuration, not a constant in
  a module.
* A result says how it was measured, so results from different prompts are not compared by accident.
* Values on a wire or in persisted state are not renamed in passing.

## Considered Options

* **Configuration keys with today's values as defaults** (`router.benchmark.prompt`, the default of
  `decision_engine.observe_light` resolved by the configuration check), plus a prompt fingerprint per benchmark result.
* Switch the benchmark prompt to English for everyone.
* One prompt per language, chosen from the client language or a locale setting.
* Leave the observation default in the module and only document it.

## Decision Outcome

Chosen option: **configuration keys with today's values as defaults**.

- `router.benchmark.prompt` (config.yaml only): the text the warm benchmark sends. Default: the earlier German prompt,
  unchanged, so measurements stay comparable with those already in `perf.json`. A nonempty text of at most 4000
  characters; anything else is refused by the configuration check. Each result records `prompt_id`, the first 12 hex
  digits of the SHA-256 of the prompt; a result without it used the default prompt.
- `decision_engine.observe_light`: when the key is absent, the configuration check sets it to `[standard, assist]`,
  limited to the names among `decision_engine.options` (other names could never match a class, so behaviour does not
  change). A list given in the configuration is taken as given; an empty list now means that no class counts as light.
  A non-list value is refused. `fallback_observation.py` reads only the resolved configuration.
- Status and log texts of core modules are English (ADR-0026). Values other code or the web UI compares against stay as
  they are (for example a catalogue source starting with `Alias von`, the client source `intern`); Home Assistant problem
  texts are changed with the structured problems, not here.

Switching the prompt to English was rejected because it would make every earlier measurement incomparable without a
gain for German-speaking installations. Per-language prompts were rejected as more machinery than the problem needs: the
operator knows the language of their clients and writes one prompt. Documenting the observation default without moving it
was rejected because a hidden role-name list keeps surprising operators whose roles are named differently.

### Consequences

* Good: operators can benchmark in the language their clients use and see from `prompt_id` which results belong
  together.
* Good: the observation default is visible in the resolved configuration; `[]` switches the classification off.
* Bad: a configuration that wrote `observe_light: []` used to get the default and now gets no light class. The key is
  not in the example configuration, and observation mode only records, so the change affects a log field, not routing.
* Bad: the default prompt is still German; operators with other languages have to set it.

### Confirmation

`test/selftest_locale_neutral.py` checks the default prompt, a configured prompt reaching every benchmark run and its
`prompt_id`, refused prompts, the resolved `observe_light` (absent, explicit, empty, unknown name, non-list, other role
names) and `would_fallback` in observation mode with the default and configured lists, and the English texts.
