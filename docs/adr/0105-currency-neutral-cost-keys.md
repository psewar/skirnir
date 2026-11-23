---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# Currency-neutral cost keys

> Refines [ADR-0014](0014-cloud-as-a-role-tier-with-declared-data-classes.md) (cloud as a role tier, budgets per
> provider). Source: the portability review of 2026-11-22 (making the router usable for other setups), router 0.37.0.

## Context and Problem Statement

A cloud provider's monthly budget is `router.cloud.providers.<p>.budget_month_chf` and a catalogue model's price is
`models.<m>.price_chf_per_m`. The router never converts currencies: prices, budgets, spend and usage costs are in whatever
currency the operator wrote the prices in. The documentation already says that the `_chf` suffix is historical, and the
example configuration carries USD list prices under `_chf` keys.

For an operator outside Switzerland the names suggest a conversion that does not happen, and nothing tells a dashboard or
the web UI which unit the numbers have. The same names are also on the wire and in persisted state:

- `roles.yaml`, written by the web UI, stores catalogue entries with `price_chf_per_m` and the settings block with the
  path `router.cloud.providers.<p>.budget_month_chf`;
- `/admin/state` and Home Assistant read `spend_month_chf` and `budget_month_chf` per provider; usage data
  (`usage.json`, `/admin/usage`) has `cost_chf`;
- Prometheus has `skirnir_cloud_spend_month_chf`, `skirnir_cloud_budget_month_chf` and `skirnir_cloud_cost_chf_total`,
  and the Grafana dashboard in the repository queries the first one.

## Decision Drivers

* An existing `config.yaml` and a `roles.yaml` written by an earlier router keep working without edits and keep their
  meaning.
* Persisted usage and spend keep their meaning; no migration of data files.
* Dashboards, alerts, Home Assistant entities and scripts that read the current names keep working.
* A configuration that gives the same value twice under two names must not silently drop one of two different values.
* The unit is visible where amounts are shown.

## Considered Options

* **Neutral configuration keys with the old names as aliases, a display-only `router.cloud.currency`, and unchanged wire,
  persisted and metric names.**
* Neutral names everywhere at once (configuration, wire fields, usage data, metrics).
* Keep the `_chf` names and only document them better.
* Convert currencies in the router (exchange rates).

## Decision Outcome

Chosen option: **neutral configuration keys with aliases and a currency setting**, because it removes the misleading names
from what operators write while nothing that reads the router today breaks.

- New keys `router.cloud.providers.<p>.budget_month` and `models.<m>.price_per_m`. `budget_month_chf` and
  `price_chf_per_m` remain accepted with the same meaning in `config.yaml`, in `roles.yaml` (catalogue entries and the
  settings path `router.cloud.providers.<p>.budget_month_chf`) and in `PUT /admin/config` (settings paths and cloud model
  bodies). `router/skirnir_router/cost_keys.py` renames them before anything else reads the tree.
- **Both names in one place:** equal values are accepted (the old name is dropped); different values are a
  configuration error (HTTP 400 for the web UI) that names both keys. The router does not pick one, because either choice
  would silently discard a value the operator wrote.
- **Layers are normalised one by one** (config.yaml, roles.yaml catalogue, roles.yaml settings, a request body) before
  they are merged. A neutral key in one layer and the old name in another are therefore not a conflict; the later layer
  wins as for every key. When the web UI writes `roles.yaml`, it writes only the neutral names, so a stored old name
  migrates with the next save.
- `router.cloud.currency` (default `CHF`, three capital letters, settable in the web UI) names the unit. It is display
  only: the provider snapshot carries it as `currency`, the web UI appends it to amounts, the log names it, and
  `skirnir_cloud_currency_info{currency="..."} 1` exposes it to Prometheus. Changing it converts nothing.
- **Wire fields, persisted fields and metric names keep their `_chf` names** (`spend_month_chf`, `budget_month_chf`,
  `cost_chf`, `skirnir_cloud_*_chf*`), and their values stay in the catalogue currency as before. The control-port views
  of the catalogue (`GET /admin/config` `models`, `/admin/state` `catalog_effective`) show `price_per_m` and, with the
  same value, `price_chf_per_m`.
- Neutral duplicates of the metrics were not added: a second copy of `skirnir_cloud_cost_chf_total` doubles its series
  (node × model × client × role) for no new information, and the info metric already tells the unit.
- Home Assistant's cost sensor keeps the unit `CHF` in this change; its discovery code is outside the router core and is
  changed separately.

### Consequences

* Good: operators write `budget_month` and `price_per_m`; nothing in their configuration claims a currency it is not.
* Good: no existing configuration, stored UI state, data file, dashboard, alert or Home Assistant entity changes.
* Good: a conflicting pair is reported with both names instead of being resolved silently.
* Bad: the wire and metric names still say `chf` for amounts that may be in another currency; readers have to look at
  `currency`. Renaming them later is a separate, breaking decision.
* Bad: two accepted names for the same key until the aliases are removed, which would need its own ADR and a release
  note.

### Confirmation

`test/selftest_locale_neutral.py` checks the old name, the new name, both with equal and with different values for the
budget and the price, the layering between config.yaml and roles.yaml in both directions, an old settings path stored by
an earlier router (applied, migrated on the next write, reset through the new path), `PUT /admin/config` with old paths
and old price bodies, the settings view (new path with the base value from an old key), and the currency (default,
valid, invalid, through the settings block, in the snapshot). The end-to-end suite runs with a test configuration that
still uses the old names, changes the budget through the old settings path and checks the metric names and the currency
info metric.
