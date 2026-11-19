---
status: proposed
date: 2026-11-19
decision-makers: operator
---

# The prompt rate counts only the tokens Ollama processed

> Refines the slowness signals of router 0.9.2 ([ADR-0022](0022-components-report-state-as-facts.md): components report
> state as facts). Source: a prompt-rate watcher in Home Assistant that raised a false alarm almost every day for three
> weeks.

## Context and Problem Statement

The router reports the prompt rate of the last warm request per node (`sensor.skirnir_node_<node>_prompt_tps`). The
documentation promised that a prompt under 200 *new* tokens does not count. The code divided `prompt_eval_count` by
`prompt_eval_duration` and applied the 200-token minimum to `prompt_eval_count`.

`prompt_eval_count` is the whole prompt, but `prompt_eval_duration` only covers the part that was not served from the
KV cache. A follow-up turn of a long conversation (90,000 tokens, all but the last few hundred cached) therefore read as
100,000 to 360,000 tok/s, while the same model processed an uncached prompt at 1,500 to 3,000 tok/s. The values fall into
two clusters two orders of magnitude apart. A baseline built from them (a weekly median in Home Assistant) depends on the
share of cache hits, not on the speed of the node, and a run of uncached prompts looked like a slowdown by a factor of 20.

Since September 2026 Ollama reports the cached part as `prompt_eval_cached_count` (absent = not known, 0 = nothing
cached; Ollama's own CLI computes its prompt rate from the processed tokens the same way).

## Decision Drivers

* A slowness signal must measure the node, not the cache hit rate of its clients.
* Report facts, do not guess (ADR-0022): a rate whose basis is unknown is not reported as if it were known.
* No configuration change, no new key; the documented behaviour stays.

## Considered Options

* **Rate over the processed tokens, HA rate only when Ollama reports the cache.**
* Keep the whole prompt and raise the minimum, or cap the rate at a plausible maximum. Rejected: a cache hit has more
  than enough tokens and its rate is not implausible for small models, so no fixed bound separates the clusters across
  models and cards.
* Remove the prompt rate. Rejected: an uncached prompt rate is the only signal for slow prefill (for example attention
  over a long context on a card that had to give up part of its memory).

## Decision Outcome

Chosen option: rate over the processed tokens.

* `perf_stats` computes `prompt_new = prompt_eval_count - prompt_eval_cached_count` (the cached count clamped to
  0..prompt, like Ollama) and the prompt rate over `prompt_new`. Without a cached count, `prompt_new` is `None` and the
  catalog keeps the previous rate over the whole prompt, so older nodes still show a value in the admin view.
* The HA rate (`Node.note_perf`) takes a prompt rate only when `prompt_new` is known and at least 200 tokens. A cache hit
  with a short new part and a node whose Ollama does not report the cache leave the last value unchanged.

### Consequences

* Good: the HA prompt rate is one cluster per model; a weekly median is a usable baseline.
* Good: the minimum of 200 tokens now applies to what the documentation always said it applied to.
* Bad: a node with an Ollama that predates the cached count no longer updates its HA prompt rate. The router updates
  Ollama on its nodes, so this is temporary; the gen rate and the RAM fact are unaffected.
* Bad: a Home Assistant baseline that already holds the old values stays skewed until they leave its window (a week with
  the documented median), unless the operator clears the history of the sensor once.

### Confirmation

`test/selftest_slowness_signals.py` checks a cache hit (184 processed of 91,684 tokens), an uncached long prompt, a
partly cached prompt, a missing cached count and out-of-range counts.
