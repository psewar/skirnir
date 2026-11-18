---
status: proposed
date: 2026-11-18
decision-makers: operator
---

# Institutional providers take their own data class limit

> Refines [ADR-0014](0014-cloud-as-a-role-tier-with-declared-data-classes.md) without superseding it. Source: the
> operator's request on 2026-11-18 to use a model server that a team's own organisation runs on its compute cluster.

## Context and Problem Statement

Under ADR-0014 every cloud provider gets data only up to `router.cloud.max_cloud_data_class` (default `internal`). A
provider's own `max_data_class` can narrow that limit, never widen it. So a request of class `personal` never leaves the
house.

Some organisations run an OpenAI-compatible model server on their own cluster. It is cleared under the
organisation's data protection rules for tasks that involve personal data, and it keeps nothing. For a team inside that
organisation it is neither a local node (it is outside the router's network, has no agent and no VRAM reports) nor a
commercial cloud (no third party gets the data). Skirnir can already call it as an ordinary OpenAI-compatible provider.
But then it gets only `internal` data, which is exactly the data the team does not need it for.

## Decision Drivers

* The default stays: without an explicit configuration nothing personal leaves the house.
* The widening must be impossible to switch on by accident or from the web UI. A stolen admin password must not open
  personal data to any provider (ADR-0016).
* Everything else that protects the cloud path applies unchanged: opt-outs, credential scan, HTTPS-only final
  destinations (ADR-0048), egress allowlist, budget, circuit breaker.
* The highest class (`secret`) never leaves the house.
* No new routing path. The provider stays a tier in the roles that are allowed to use it.

## Considered Options

1. **A per-provider trust level** `trust: cloud | institutional` (default `cloud`), set only in `config.yaml`. An
   institutional provider's own `max_data_class` replaces the global limit, up to the class below the highest.
2. Let any provider's own `max_data_class` widen the global limit.
3. Raise the global `max_cloud_data_class` to `personal`.
4. Treat the server as a node (static `nodes:` entry or an agent in front of it).
5. A separate trust zone with its own global limit, execution mode and client opt-in (`max_institutional_data_class`,
   `routing.execution: institutional`, `clients.<c>.institutional`).

## Decision Outcome

Chosen option: **1**, because it changes only the data class limit, and only for a provider the operator marked in the
configuration file. It reuses everything the cloud path already enforces.

* `router.cloud.providers.<p>.trust` is `cloud` (default) or `institutional`. Anything else, and an institutional
  provider with the highest data class as `max_data_class`, stops the configuration from loading. `trust` is not in
  `settings.EDITABLE`. The web UI can still change `max_data_class`, but the same check refuses the highest class there.
* An institutional provider with its own `max_data_class` receives data up to that class. Without one it gets the
  global limit, like any provider. A provider with `trust: cloud` keeps the old rule: its own class only narrows. A
  higher value has no effect, and the router logs a warning at start. It is not refused, so that lowering the
  global limit in the web UI never fails because of it.
* The request-level gate lets a class through up to the widest limit of an *enabled* provider. The per-provider gate
  then closes the providers not cleared for it, with a reason naming the provider and its trust.
* The content check of ADR-0038 runs only for a provider whose limit is below the request's class. An institutional
  provider cleared for `personal` gets the request without the check and without the redaction.
* Opt-outs close institutional tiers like any cloud tier: `routing.execution: local`, `clients.<c>.cloud: false`, memory
  projects with `allow_cloud: false` and managed sessions. The data still leaves the router's network.
* `/admin/state` and the web UI report each provider's `trust` and its effective `ceiling` as facts.

Option 2 was rejected because a value that used to narrow would silently start widening. That includes a value typed
for a commercial provider, or one changed in the web UI. Option 3 was rejected because it opens personal data to
every provider at once. Option 4 was rejected because the cloud protections (credential scan, opt-outs, data classes,
egress allowlist) do not apply to nodes, and a remote server is not a node the router can measure or manage. Option 5
was rejected for now: it adds configuration keys and a client opt-in whose behaviour the existing opt-outs and per-client
role grants already cover. Which clients may use the institutional provider is decided by the roles it is a tier in
(`clients.<c>.roles`).

### Consequences

* Good, because a team can send personal data to its organisation's server, and nothing else changes for other
  providers or existing configurations.
* Good, because the widening is visible: one key in `config.yaml`, the trust and ceiling in the UI, the start log line,
  and the provider in every skip reason.
* Bad, because the router cannot verify the organisation's promise (no storage, data protection). The operator vouches
  for it by setting `trust: institutional`.
* Bad, because "personal" is not tied to whose data it is. Household data in a role that falls back to an institutional
  tier would go to the organisation. The documentation advises a dedicated role granted only to the clients whose data
  the organisation may process.

### Confirmation

`test/selftest_institutional_provider.py` (in CI) covers the accepted cases and the rejected ones. Accepted: personal
data to the institutional tier only, the content check skipped there. Rejected: unknown trust, the highest class
(also from the web UI and with a custom class list), trust set from the web UI, `routing.execution: local`,
`cloud: false`, the credential scan, a narrower own class, an institutional provider without its own class, a disabled
provider, an ordinary provider with a higher own class. It also checks the snapshot facts and the wiring in
`proxy._via_cloud`. Each gate was broken in turn by fault injection, and the test failed each time.
