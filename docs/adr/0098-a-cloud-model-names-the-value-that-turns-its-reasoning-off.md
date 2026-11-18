---
status: proposed
date: 2026-11-18
decision-makers: operator
---

# A cloud model names the value that turns its reasoning off

> Source: a probe of an OpenAI-compatible endpoint served by vLLM 0.31 with a reasoning model, configured as a cloud
> provider (`kind: openai`), 2026-11-18.

## Context and Problem Statement

A client turns thinking off with Ollama's `think: false`. For a cloud model whose catalog entry says `reasoning: true`,
the `openai` adapter translates that into `reasoning_effort: "minimal"`, the lowest effort of OpenAI's gpt-5 models
(without it gpt-5-mini spends about 130 thinking tokens on an "OK"). `think: true` or no `think` sends nothing and leaves
the provider's default.

The probe of a vLLM-served reasoning model through the same adapter showed:

| Request | Thinking | Answer |
|---|---|---|
| no `reasoning_effort` | yes, about 200 tokens, about 1 s | complete |
| `reasoning_effort: "minimal"` (what the router sends today) | yes, unchanged | a small `max_tokens` ends with `finish_reason: length` and empty content |
| `reasoning_effort: "none"` | no | complete |
| `chat_template_kwargs: {enable_thinking: false}` | no | complete |

Which value turns reasoning off is a property of the model and the server that serves it, not of the client or of the
router. One provider endpoint can serve models that need different values.

The same servers return the thought in `message.reasoning` (and `delta.reasoning` when streaming). The adapter only read
`reasoning_content`, the field llama.cpp-style servers use (router 0.10.0, OpenAI-compatible nodes), so it dropped the
thought of such a model even when the client asked for it.

## Decision Drivers

* An existing configuration behaves exactly as before without edits; gpt-5 models keep getting `"minimal"`.
* The value is set where the model is described, per model, since one endpoint can serve models that differ.
* A key that would do nothing, or a value the router does not know, is refused by the configuration check, not ignored.
* For the client the contract stays Ollama's: `think: false` means no thinking; otherwise the thought arrives as
  `thinking`.
* Prefer the standard OpenAI request field over server-specific extensions where the standard one works.

## Considered Options

* **A per-model catalog key `reasoning_off: minimal | none`** next to `reasoning: true`, default `minimal`.
* A per-provider key (`router.cloud.providers.<p>.reasoning_off`).
* Always send `"none"` for `think: false`.
* Send `chat_template_kwargs: {enable_thinking: false}` for such models.
* Let `reasoning` itself take the value (`reasoning: none`).

## Decision Outcome

Chosen option: **a per-model catalog key `reasoning_off`**.

- `models.<m>.reasoning_off` is the `reasoning_effort` value the router sends when the client asks `think: false`:
  `minimal` (the default when the key is absent, today's behaviour) or `none`. `think: true` or no `think` still sends
  nothing.
- It only means something together with `reasoning: true` on a cloud model (`cloud: <provider>`). The configuration
  check refuses any other value, the key without `reasoning: true`, and the key on a local model (an OpenAI-compatible
  node always gets `"none"` for `think: false`, since router 0.10.0). `PUT /admin/config` and the web UI keep the key
  when they write a cloud model, and the same check applies there.
- The answer's thought: the adapter reads `reasoning_content`, and if that is absent or empty, `reasoning`. It takes one
  of them, never both, so a server that sends the same text under both names does not double it. The thought becomes
  Ollama's `thinking` in unary and streamed answers, for cloud providers and OpenAI-compatible nodes alike (they share
  the adapter).
- A client that sent `think: false` gets no thought, also when the provider thinks anyway (a model that ignores the
  value): the client said it wants none, as an Ollama node then returns none. Without `think` the thought is returned,
  as before for `reasoning_content`; on an Ollama node, too, only `think: false` keeps a thinking model's thought out of
  the answer.

A per-provider key was rejected because the difference is between models: a provider (or a gateway in front of several
model families) can serve one model that takes `minimal` and one that ignores it, and the catalog already says per model
whether it reasons. Always sending `"none"` was rejected because it changes what every existing configuration sends, and
providers accept different sets of values per model; a value a model does not take is an HTTP 400 from the provider.
`chat_template_kwargs` was rejected as the default mechanism: it is a server extension (vLLM, SGLang), and the template
variable's name depends on the model's chat template; the standard `reasoning_effort: "none"` worked in the probe. It can
become a further value of `reasoning_off` if a model ever needs it. `reasoning: none` was rejected because it reads as
"does not reason", the opposite of its meaning, and changes the type of a key that the web UI writes as a boolean.

### Consequences

* Good, because a vLLM-served reasoning model answers `think: false` without thinking: no extra second and about 200
  tokens per request, and no empty answer when the client sets a small `num_predict`.
* Good, because such a model's thought reaches clients that want it (`reasoning` in the OpenAI shape, a thinking block in
  the Anthropic shape) instead of being dropped.
* Good, because existing configurations are unchanged: no key means `minimal`, as before.
* Bad, because the operator has to know which value a model needs. A wrong `minimal` shows only as thinking time and
  tokens; the documentation says how to check it (a `think: false` request with a small `num_predict`).
* Neutral: a client that sent `think: false` no longer receives a thought that a provider produced anyway. An
  OpenAI-compatible node that sends `reasoning` instead of `reasoning_content` now also has its thought returned.

### Confirmation

`test/selftest_cloud_reasoning.py` sends requests through `cloud.run` to a fake OpenAI-compatible provider (no network)
and checks: the default `minimal` and the configured `none` for `think: false`; nothing for `think: true`, for no `think`
and for a model without `reasoning: true`; the configuration check refuses an unknown value, the key without
`reasoning: true` and the key on a local model, and accepts the example configuration; `PUT /admin/config`'s model entry
keeps the key; the thought from `reasoning` and from `reasoning_content`, unary and streamed, not doubled when both are
sent, and dropped for `think: false`. In operation: with `reasoning_off: none` on the vLLM-served model, a `think: false`
request with `num_predict: 64` answers with content, no `thinking` and an `eval_count` close to the answer's length.
