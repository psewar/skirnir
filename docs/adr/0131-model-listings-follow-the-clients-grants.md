---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# Model listings follow the client's grants

> Extends [ADR-0052](0052-concrete-model-names-need-an-explicit-grant.md) (concrete model names need an explicit grant)
> from requests to listings. Threat: TM-04.

## Context and Problem Statement

Since ADR-0052 a client needs `models: true` to call a concrete model, and `roles` limits which roles it may call.
The listings did not follow: `/api/tags`, `/v1/models` (OpenAI and Anthropic form) and `/v1/models/{model}` showed every
role and, with `expose_concrete_models`, every concrete model of every node to every client. A role-only client saw
model and role names it would get 403 for. That is information, not access (TM-04's open validation gap), but it tells
a limited client which models and hardware the house runs, and a client such as Home Assistant offers the user entries
that then fail.

## Decision Drivers

* A listing should name what the client may use, so clients do not offer what fails.
* The request path stays the only authority; a listing must never grant anything.
* Listing is not a refused request: it must not count `forbidden` or write audit lines.
* Unauthenticated observe mode keeps its meaning (no identity, no restriction).

## Considered Options

* **Filter every listing with the same grant check as the request path, without its side effects.**
* Keep listing everything (status quo). Rejected: the listed entries fail with 403 for a limited client.
* A per-client model allowlist (the risk register's other idea). Not now: it is a new grant beyond `roles`/`models`; the
  filter here works with the grants that exist and would apply an allowlist the same way later.

## Decision Outcome

Chosen option: "filter every listing with the grant check", because it makes the listing agree with what the request
path allows, with one rule in one place.

* `auth.client_grant(request, role, concrete)` is the grant check without side effects; `auth.client_allows` (request
  path) calls it and counts and audits a refusal as before.
* `proxy.tags_list(request)`, behind `/api/tags`, `/v1/models` and `/v1/models/{model}`, lists a role only when
  `client_grant` allows it, concrete model names only when the client may use concrete names (`models: true`, or the
  router's own UI client) and `expose_concrete_models` is on, and the decision router's `auto` model only when at least
  one role is listed.
* Unauthenticated requests in observe mode see everything, as they may use everything.

### Consequences

* Good, because a client sees exactly what it may call; a role-limited Home Assistant no longer offers models that fail.
* Good, because the request path is unchanged: a hidden role or model is still refused (403) and audited when asked for.
* Bad, because `expose_concrete_models` no longer shows concrete names to a client without `models: true`; an operator
  who relied on seeing them there must grant `models: true` (which the client needed to use them anyway).
* Neutral: `/api/ps` and `/api/show` are unchanged; `/api/show` of a role answers as before.

### Confirmation

`test/selftest_model_listing.py` (in CI) over the real API app: a client limited to one role sees only that role, a
client with all roles but `models: false` sees no concrete name, `models: true` adds them; `/v1/models` in OpenAI and
Anthropic form filters alike, `/v1/models/{model}` answers 404 for a concrete model the client may not use; observe
mode without identity and the router's UI client see everything; listing counts no forbidden request and writes no audit
line; a hidden role is still refused with 403 and audited when requested (rejected case). Without the change the
filtering tests fail.
