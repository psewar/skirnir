---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The running-model list follows the client's grants

> Extends [ADR-0131](0131-model-listings-follow-the-clients-grants.md) (model listings follow the client's grants),
> which left `/api/ps` unchanged. Threat: TM-04.

## Context and Problem Statement

After ADR-0131, `/api/tags` and `/v1/models` show a client only the roles it may use and concrete names only with
`models: true`. `/api/ps` still answered every client with every loaded model: the role it belongs to, the concrete
model name and the node it is loaded on. A role-only client learned the concrete models and the node names the listings
now hide.

## Decision Drivers

* The same rule as ADR-0131, one grant check (`auth.client_grant`), no counting or audit for a listing.
* `/api/ps` keeps the Ollama shape (`name`, `model`, sizes) that clients such as Open WebUI read.
* Unauthenticated observe mode and the router's UI keep the full view.

## Considered Options

* **Filter `/api/ps` by the grants; a role-only client sees its roles by role name, without model or node.**
* Refuse `/api/ps` to clients without `models: true`. Rejected: clients that show running models would break for
  role-only clients, although knowing that a granted role is loaded is harmless and useful (warm or cold).
* Keep it unchanged. Rejected: it undoes ADR-0131 for the concrete names and the node names.

## Decision Outcome

Chosen option: "filter `/api/ps` by the grants", because it applies ADR-0131's rule to the last listing that ignored it.

* A client that may use concrete names (`models: true`, the router's UI client, observe without identity) sees every
  loaded model with `model` and `node`, as before; an entry whose role it may not use is named by the concrete model,
  not by that role.
* A role-only client sees one entry per loaded model of a role it may use, with `name` and `model` set to the role and
  no `node`; models of other roles and models outside any role are left out.

### Consequences

* Good, because no listing names a concrete model or a node to a client that may not use concrete names.
* Good, because a role-only client still sees whether its roles are warm.
* Bad, because a role-only client that read node names from `/api/ps` no longer gets them.

### Confirmation

`test/selftest_model_listing.py` (in CI): with a node that has loaded a concrete model outside any role and models of two
roles, `models: true` sees all three with model and node; a one-role client without `models: true` sees only its role,
named by the role, without model or node; all roles without `models: true` see both roles that way and no concrete
model; `models: true` with one role sees the other role's model under its concrete name; observe without identity sees
everything; nothing is refused. Without the change the test fails.
