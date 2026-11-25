---
status: proposed
date: 2026-11-25
decision-makers: maintainer (asked for by kay, proposed by Claude)
---

# A client may be granted a list of concrete models

> Supersedes [ADR-0052](0052-concrete-model-names-need-an-explicit-grant.md) (concrete model names need an explicit
> grant), which named per-client allowlists as a possible later refinement. Everything else of ADR-0052 stays: the
> default denies, a missing setting warns at load time, the router's UI client keeps every name, and
> `expose_concrete_models` decides whether concrete names are routed at all. Threat: TM-04 (next action "consider
> per-client model allowlists").

## Context and Problem Statement

Since ADR-0052 a client either may call every concrete model name (`models: true`) or none (`false`, roles only). An
integration that needs one specific model (a benchmark, an embedding model, a tool that pins its model) had to get all
of them, including the largest and most expensive models of every node. Since ADR-0131 and ADR-0133 the listings follow
the same all-or-nothing grant.

## Decision Drivers

* Grant exactly the concrete models a client needs (least privilege, TM-04).
* Existing configurations keep working without edits: `true` and `false` keep their meaning.
* One parser and one check for configuration, web UI and request path, so they cannot drift apart.
* The listings show what the client may call (ADR-0131, ADR-0133).

## Considered Options

* **`models` takes `true`, `false` or a list of concrete model names; a listed name (tag-normalised) may be called,
  listed and shown loaded.**
* A separate key (`model_allowlist`) next to the boolean. Rejected: two keys for one grant invite contradictions
  (`models: false` with a list) and need a precedence rule.
* Patterns (`qwen*`). Rejected for now: a pattern grants models that appear later without the operator seeing them;
  exact names keep the grant reviewable.
* Keep the boolean (ADR-0052). Rejected: that is the gap TM-04 names.

## Decision Outcome

Chosen option: "true, false or a list", because it adds the narrow grant without a new key and leaves every existing
configuration as it was.

* `values.model_grant` parses the value for the configuration, the settings (web UI) and the UI's client creation:
  `true`, `false`, or a list of at most 200 concrete model names (letters, digits and `._:/@+-`); an empty list is
  `false`; anything else is refused with the key named. A non-boolean, non-list value (for example `"yes"`) stays a
  configuration error, as in ADR-0052.
* `values.model_granted` matches a concrete name against the grant; a name without a tag means `:latest` on both sides,
  as Ollama resolves it.
* `auth.client_grant` (request path through `client_allows`, and the listings) allows a concrete model only when it is
  granted; a refusal names the model and the allowlist ("concrete model X not in its models allowlist") and is counted
  and audited as before.
* `/api/tags`, `/v1/models` and `/v1/models/{model}` list, and `/api/ps` shows with model and node, only the concrete
  models the client may call.
* The web UI edits the grant as text: `*` = all, empty = none, otherwise a comma list.

### Consequences

* Good, because a client can get exactly the concrete models it needs.
* Good, because `true` and `false` mean what they meant; no configuration changes.
* Bad, because a listed name that no node offers yet is accepted (models come and go); a misspelt name grants nothing
  and the client gets 403 with the name in the reason.
* Neutral: the UI API's client creation no longer reads a string such as `"yes"` as `true` for `models`; the web UI never
  sent one.

### Confirmation

`test/selftest_model_allowlist.py` (in CI): the parser accepts true, false and name lists (trimmed, deduplicated, empty =
false) and refuses strings, numbers, bad names and too long lists with the key named; tag normalisation; settings and UI
client creation share the parser; the configuration takes a list and refuses `"yes"`; a listed model passes the request
path, another is refused with the allowlist reason and audited (rejected case), the UI client and unidentified requests
are unchanged; `/api/tags`, `/v1/models` and `/api/ps` show only the listed concrete model. `test/selftest_security.py`
keeps ADR-0052's cases (missing setting, `true`, a non-boolean string). Without the change the new tests fail.
