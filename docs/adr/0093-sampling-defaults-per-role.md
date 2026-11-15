---
status: proposed
date: 2026-11-15
decision-makers: operator
---

# Sampling defaults per role

> Source: measurements on a Windows node serving a 2-bit mixture-of-experts model through an OpenAI-compatible server
> (Strata 0.1.59) as the first tier of a voice-assistant role, 2026-11-15.

## Context and Problem Statement

Home Assistant's Ollama integration sends no sampling options with its requests (no temperature, no penalties). The
model then samples with whatever its server defaults to. On an Ollama node that is the model file's `PARAMETER` block;
on an OpenAI-compatible server it is the server's own default, for Strata the model card's recommendation for
non-thinking use (temperature 0.7, top_p 0.8, top_k 20, presence_penalty 1.5).

Measured through the voice pipeline with one fixed question that needs two tool calls and a short calculation:

| First tier, sampling | Answered | Tool calls | Time |
|---|---|---|---|
| MoE, 2-bit, server default (model card) | 5 of 12 | 2, or 20 and the client gives up | 2-25 s |
| MoE, 2-bit, temperature 0 | **6 of 6** | always 2 | 1.8-4.2 s |
| dense 27B, Q4_K_M, Ollama model file (temperature 1, presence 0) | 6 of 6 (one value invented) | 0-2 | 1.4-3.4 s |

The failures were not memory pressure (repeated with free RAM, same rate) but the model calling the tool room by room
with invented names until the client's iteration limit. Which sampling a model needs is a property of the model and the
role together: a voice role wants short deterministic tool use, a coding or agent role may want the card's sampling, and
the same model serves several roles. The client cannot be asked to send it, and the server's default applies to every
role at once.

The router also dropped every sampling key except `temperature`, `top_p`, `seed` and `stop` on the way to an
OpenAI-compatible node, so a client's `top_k` or `presence_penalty` never reached such a server.

## Decision Drivers

* The fix must apply to one role without changing the model's behaviour for the other roles that share it.
* A client that sends its own sampling keeps it.
* Cloud tiers must keep working: gpt-5 models refuse a temperature other than their default.
* An existing configuration keeps working unchanged; a typo must not silently do nothing.

## Considered Options

* **Sampling defaults per role** (`roles.<r>.options`), filled in by the router for local tiers.
* Sampling defaults per tier (`tiers[].options`).
* The server's global default (Strata's shared chat settings or its run-config `sampling` block).
* Ask clients to send sampling options.

## Decision Outcome

Chosen option: **sampling defaults per role**.

- `roles.<r>.options` takes Ollama option names: `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`,
  `frequency_penalty`, `repeat_penalty`. The configuration check refuses unknown keys, wrong types and values out of
  range (temperature 0-2, top_p and min_p 0-1, top_k 1-1000, presence and frequency penalty -2-2, repeat penalty 0-2).
- For a request to the role, the router fills each key the client left out into the request's options for a **local**
  tier (Ollama node or OpenAI-compatible node). A cloud tier never gets them; a request for a concrete model gets none.
- To an OpenAI-compatible node the router now also passes `top_k`, `min_p`, `presence_penalty`, `frequency_penalty` and
  `repeat_penalty` (as `repetition_penalty`), whether they come from the role or the client. Cloud providers get only
  what they got before.
- Like `priority`, the key belongs in `config.yaml`; `PUT /admin/config` passes it through, and `GET /admin/config`
  shows it.

Per tier was rejected because the web UI and the role tools write tiers as `{model, num_ctx, busy_ok}` and would drop it
on every save, and because the need follows the role's use more than the tier's position. The server's global default
was rejected because it changes every role at once (and Strata's shared chat settings do not take a presence penalty).
Asking the client is not possible for Home Assistant's integration.

### Consequences

* Good, because one role can be made deterministic for tool use while the other roles keep the model card's sampling.
* Good, because a voice role on a model that loops at its card's sampling no longer depends on chance.
* Good, because sampling keys sent by a client now reach OpenAI-compatible nodes instead of being dropped silently.
* Bad, because the same role options also apply to a fallback tier with a different model, which may prefer other values;
  operators choose values that suit every local tier of the role.
* Bad, because greedy decoding (temperature 0) can repeat itself in long outputs; it suits short tool-use roles, not
  long generation.
* Neutral: a client that already sent `top_k` or penalties to a role whose tier is an OpenAI-compatible node now gets them
  applied there.

### Confirmation

`test/selftest_role_options.py` checks the validation (including the refused cases), that a local tier gets the defaults
and the client's value wins, that a cloud tier and a concrete model get none, that the override path keeps them and an
invalid override changes nothing, and what an OpenAI-compatible node and a cloud provider receive. In operation, the
router journal names the tier of each request; the same voice question is repeated against the role after setting
`options`.
