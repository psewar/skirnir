---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude from the model test bench's wishes)
---

# A read-only admin endpoint lists each role as model at context

## Context and Problem Statement

Tools outside the router need to know which model each role runs at which context. One example is the model radar,
which compares candidates with the incumbents. Today they read `GET /admin/config`. That answer holds the whole editable
configuration, including the roles' prompts (`system_append`), options, the client list and the nodes. A tool that needs
one mapping receives all of it, and its parser depends on a large answer that changes with every new setting.

## Decision Drivers

* A small, stable answer for "role → model@context".
* Nothing in it that the tool does not need, above all no prompts.
* A tool that reads `roles` from `GET /admin/config` today switches by changing only the URL.
* No new way in: the same admin login and network limit as every admin path.

## Considered Options

* **Chosen: `GET /admin/roles` on the control port.** It answers `{version, roles}`. `roles` has the shape of `roles` in
  `GET /admin/config`, reduced to `exposed_as` and, per tier in order, `model`, `num_ctx` and, for a cloud tier,
  `cloud`.
* **The same listing on the API port for clients.** Rejected: model listings for clients follow their grants
  ([ADR-0131](0131-model-listings-follow-the-clients-grants.md)). The concrete models behind the roles are operator knowledge.
* **A read-only admin user that may see all of `GET /admin/config`.** Rejected for now: it would still hand out the
  prompts, and the router has no admin roles yet. It may come later as its own decision.
* **Keep reading `GET /admin/config`.** Possible, but it leaves the tool depending on the whole configuration.

## Decision Outcome

Chosen option: "`GET /admin/roles`".

* It is read only: there is no `PUT`, and roles change through `PUT /admin/config` as before.
* It is behind the admin login and `router.admin_networks` like every control path outside `/v1/`
  ([ADR-0143](0143-the-admin-part-of-the-control-port-answers-only-configured-networks.md)).
* The answer carries `version` (the router version) and `Cache-Control: no-store`.

### Consequences

* Good, because a tool gets exactly the mapping, with no prompts and no other settings.
* Good, because the shape matches `roles` in `GET /admin/config`, so existing parsers keep working.
* Bad, because it is one more admin path to keep in step with the roles' shape. The self-test compares the two.
* Neutral: it needs the same admin credentials as before. A separate, narrower credential is not part of this.

### Confirmation

`test/selftest_bench_api.py`:

* Every role of the example configuration appears with the same `exposed_as` and the same tiers, model and `num_ctx` in
  order, with `cloud` where the tier has it.
* There is nothing else: no `system_append`, options, canary or shadow.
* The route is GET only and answers `{version, roles}` with `no-store`.
* Without a login the admin middleware answers 401 before the handler runs.
