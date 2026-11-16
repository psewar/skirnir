---
status: proposed
date: 2026-11-16
decision-makers: operator
---

# System text per model of a role

> Source: a voice-assistant role with a dense 27B model and a 2-bit mixture-of-experts model (served by an
> OpenAI-compatible server) as its local tiers, 2026-11-15/16.

## Context and Problem Statement

Clients such as Home Assistant send one system prompt per conversation agent, written for whatever model answers. Some
rules in such a prompt are only needed because of one model's weaknesses, and they can make another model worse:

* A rule "if the user asks about one room, query exactly this room and stop when it has no value" fixed a search loop
  of the mixture-of-experts model (loops in 1 of 12 runs instead of 5 of 8). The same rule made that model query room
  by room for questions about the whole house (0 of 5 answered, stopped by the client's guard after 8 lookups; before
  the rule 6 of 6 with 2 lookups). The dense model answered 55 of 55 questions with the rule.
* Other rules were measured on a single model only (numbers as digits because one model misspells German number words,
  values only from a tool result because another model invented them).

The client cannot know which model answers. The router picks the tier per request: a warm model wins over a higher
ranked cold one, a busy node skips tiers, a canary takes a share. Measured on 2026-11-16: the role listed the dense
model first, but all 42 calls of a test went to the mixture-of-experts model, because it was loaded and the dense model
was only in the node's standby store. A rule keyed to "the configured first model" would have been applied to the wrong
model in every one of them.

## Decision Drivers

* A model-specific rule must reach exactly the model it was written for, also after a fallback, on every rung of a
  context ladder and for a canary of that model.
* Other models of the role and other roles using the same model must not see it.
* Tools that rewrite a role's tiers (web UI, role scripts) must not silently drop it.
* Existing configurations keep working; the client's own messages are not changed for anything but the request sent to
  that model.

## Considered Options

1. **Per role and model in the router** (`roles.<r>.system_append: {model: text}`), appended when that model serves.
2. Per tier in the router (`tiers[].system_append`).
3. In the client's prompt, conditional on the role's configured first model (a template reading the router's
   configuration).
4. In a wrapper agent in front of the client's conversation agent.

## Decision Outcome

Chosen option: **1, per role and model in the router** (proposed by the operator on 2026-11-16: "the rule should only
apply when exactly that model is used").

* `roles.<r>.system_append` maps catalogue models to text (at most 8000 characters each; blank text means none). A model
  missing from the catalogue, a non-text value or an over-long text stops the configuration from loading, like an
  unknown key.
* When a request for the role is served by a model that has an entry, the router appends the text to the request's
  leading system message (separated by a blank line), or puts it in a new leading system message when there is none or
  its content is not plain text. The client's own body is not modified, so the next tier starts from the original.
* Only chat requests (`/api/chat` and the OpenAI and Anthropic chat shapes, which the router turns into the same native
  form) get it; `/api/generate`, embeddings and requests for a concrete model do not.
* Every tier with that model gets the text (context ladder, fallback, canary); no other model does.
* The route entry in the router journal records the appended length (`system_append`), never the text.
* Like `options` (ADR-0093) the key belongs in `config.yaml`: a role saved from the web UI or by a tool that writes only
  tiers keeps it; `PUT /admin/config` takes it when sent, `GET /admin/config` shows it.

Per tier (2) was rejected: the text would follow a tier's position, not the model (an operator changing a tier's model in
the UI would keep a rule written for the old one), a context ladder would need the same text twice, and the role tools
write tiers as `{model, num_ctx, busy_ok}` and would drop it on every save. The client-side options (3, 4) were rejected
because only the router knows which model serves a request; the measurement above shows how often the configured first
model is not the one that answers.

### Consequences

* Good, because a rule one model needs no longer changes another model's behaviour, and a model switch in the role does
  not require rewriting the client's prompt.
* Good, because a rule records in configuration which model it was written for; the client's prompt keeps the rules every
  model needs.
* Bad, because the effective prompt is now split between the client and the router; whoever reads the client's prompt
  alone does not see the model's additions. The route journal names the model and whether text was appended.
* Bad, because changing the appended text changes the prompt prefix after the client's system prompt only for that model;
  a prompt cache for the client's prefix stays valid, the part after it is recomputed once.
* Neutral: the text goes to a cloud tier like the client's own system prompt when that cloud model has an entry; the
  privacy check classifies user messages, not system text, so the operator writes no personal data into it (TM-05).

### Confirmation

`test/selftest_system_append.py` checks the validation (including the refused cases: unknown model, non-text, over-long,
wrong shape), that an invalid override changes nothing, that a role save without the key keeps `config.yaml`'s text,
the `GET`/`PUT /admin/config` round trip, that exactly the serving model gets the text on every tier with that model and
as a canary, and that another model, a concrete-model request and `/api/generate` get none, with the client's body left
untouched. In operation, the route journal's `system_append` length shows per request whether the text was applied; the
same question set is measured against each model alone before and after moving a model-specific rule out of the
client's prompt.
