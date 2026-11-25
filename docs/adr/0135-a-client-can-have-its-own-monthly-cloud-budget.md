---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# A client can have its own monthly cloud budget

> Extends [ADR-0014](0014-cloud-as-a-role-tier-with-declared-data-classes.md) (cloud as a role tier, with a monthly
> budget per provider) to clients. Threat: TM-08 (next action "per-client cost bounds for cloud").

## Context and Problem Statement

Cloud tiers cost money per token. The router bounds the spend per provider and month (`budget_month`): once a
provider's budget is used up, it drops out as a tier for everybody. Nothing bounded what one client could spend. A
client allowed to use the cloud (a script in a loop, a misconfigured agent, a leaked token) could use up a provider's
whole monthly budget, and with it the cloud for every other client, before anyone noticed. Per-client rate and
concurrency limits (ADR-0063) bound the request rate, not the cost of long cloud answers.

## Decision Drivers

* A client's cloud spend can be capped without touching other clients or the provider budgets.
* No new store: the router already records each finished request's cost per client and day (`usage.json`).
* Same behaviour as the provider budget: the cloud tiers are skipped with a reason, local tiers continue.
* An existing configuration keeps working unchanged.

## Considered Options

* **`client_auth.clients.<c>.cloud_budget_month`, the client's spend over all providers in the calendar month, summed
  from `usage.json`; reached = its cloud tiers are skipped.**
* A budget per client and provider. Rejected for now: more keys for a rare need; the provider budget already caps each
  provider, and the client budget caps the client.
* Reject the request (429/402) when the budget is used up. Rejected: the router's answer to a closed cloud tier is to
  stay local (ADR-0014); a client should get the local answer, not an error.

## Decision Outcome

Chosen option: "a per-client monthly budget over all providers", because it caps what matters with the data the router
already keeps.

* `cloud_budget_month` (number 0-100000, in the catalogue currency; 0 or absent = no limit). The configuration refuses
  other values; the web UI may change it (Clients).
* `cloud.client_spend_month(client)` sums the client's `cost_chf` over the days of the current month in `usage.json`
  (`metrics.usage_add`, fed by every finished request; kept 90 days).
* `cloud.request_block_reason` returns `client <c>: monthly cloud budget <n> exhausted` once the spend reached the
  budget, after the client's own opt-out; every cloud tier of the request is then skipped with that reason, local tiers
  continue.
* `/admin/state` shows `cloud_budget_month` and `cloud_spend_month` per client.

### Consequences

* Good, because one client can no longer use up a provider's budget for everyone.
* Good, because no new file or counter: the spend is the one already shown in usage and metrics.
* Bad, because requests already running when the budget is reached still finish and are charged; concurrent requests
  can overshoot by what is in flight, as with the provider budget.
* Bad, because a month's spend counts what `usage.json` holds; deleting that file resets it (it also resets the usage
  view).

### Confirmation

`test/selftest_client_cloud_budget.py` (in CI): the spend sums only this month's cost of this client; below the budget
the cloud stays open, at the budget the request's cloud block reason names the exhausted budget (rejected case),
clients without a budget or with 0 and unidentified requests are not limited; the client view shows budget and spend;
the configuration refuses negative, too large, string, boolean and list values. Without the change the tests fail.
