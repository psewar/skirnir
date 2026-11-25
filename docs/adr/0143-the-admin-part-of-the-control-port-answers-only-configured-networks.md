---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude while working through the open risk-register items)
---

# The admin part of the control port answers only configured networks

> Threat: TM-03 (next action "Segment admin access"). Same pattern as
> [ADR-0136](0136-new-agents-enroll-only-from-configured-networks.md) (new agents enroll only from configured networks).

## Context and Problem Statement

The control port (11435) serves two audiences. The agents use `/v1/tunnel`, `/v1/agent/binary/...` and
`/v1/heartbeat/...`. The operators use the web UI and its files, `/admin/*` and `/metrics`.

Remote agents need the port on a network address
([ADR-0040](0040-authentication-fails-closed-control-port-on-loopback-by-default.md)). So the web UI and the admin API
answer every network that reaches the agents, the GPU nodes included. A compromised node (TM-07), or anything else on
those networks, can try passwords there or use a stolen admin login. Password attempts are limited to five failures per
address and minute, then the router answers 429.

A firewall can open the port to the nodes and the admin machines, but it cannot limit paths: it cannot let the nodes
reach the tunnel and keep them off the admin UI. Only the router sees the path.

## Decision Drivers

* The agents keep reaching their paths from their networks. Nothing changes for an installation that does not opt in.
* Refuse before the login, so an address outside gets no password attempt.
* A stolen admin login must not be able to widen the list.
* A mistake that would lock out every admin is refused when the configuration loads.
* The posture report names the open state, so an operator sees it before a rollout (TM-01).

## Considered Options

* **`router.admin_networks`**: a CIDR list, checked in the control port's middleware for every path outside `/v1/`.
  Empty or absent means any address. Set in the configuration file only, with a posture finding.
* **A second listener for the admin part** (`admin_listen`), which the operator binds to a management interface.
  Rejected for now: it adds a second socket, TLS on both sockets, reverse proxy setups, `install.sh`, and the
  container's ports and health check. An address list gives the same separation on one port. A second listener stays
  possible later, and the list would still apply then.
* **Leave it to the firewall or a reverse proxy.** Rejected as the only control: without TLS termination neither sees
  paths, and the agents need the same port.
* **Make the list editable in the web UI.** Rejected: a stolen admin login could widen it, and a mistake there locks the
  operator out of the UI that would undo it.

## Decision Outcome

Chosen option: "`router.admin_networks`", because it separates the two audiences of one port where the path is known,
before any login, and keeps every existing configuration working.

* **The check.** `auth.basic_auth_middleware` refuses a request whose path does not start with `/v1/` and whose source
  address lies outside every listed network. The answer is 403
  `admin access is not allowed from this address (router.admin_networks)`. The check runs before the Basic Auth check,
  before the unauthenticated loopback switch
  ([ADR-0053](0053-control-port-requires-users-even-on-loopback.md)) and before the failure counter. An IPv4-mapped
  IPv6 address counts as its IPv4 address (`values.in_networks`, as in ADR-0136).
* **`/v1/` paths keep their own checks**: tunnel identity and approval, the enrollment networks of ADR-0136, and the
  heartbeat token. aiohttp matches routes on the literal path and does not fold dot segments, so no `/v1/` path
  resolves to an admin handler. A test pins the set of `/v1/` routes on the control port, so a new one must be decided.
* **The configuration refuses** a list that is not CIDR networks. It also refuses a non-empty list without a loopback
  address when `control_listen` is literal loopback, because every admin request, also through a local reverse proxy,
  would be refused.
* **Where it is set.** The key is not in `settings.EDITABLE`. It is changed in `config.yaml` and applies on reload.
* **Posture.** The report gives `admin_from_any_network` (info, TM-03) when the control port listens on a network
  address and the list is empty. It is information like `control_on_network`, not medium: it is a hardening
  step, and `router.py --init` cannot know the admin networks and keeps writing a configuration without medium
  findings ([ADR-0067](0067-guided-first-configuration.md)).

### Consequences

* Good, because the GPU nodes, and other networks that reach the tunnel, no longer reach the admin login.
* Good, because nothing changes without the key; existing installations keep working.
* Bad, because behind a reverse proxy, or a container runtime's userland proxy, the router sees the proxy's address. The
  proxy then has to do the segmentation (documented).
* Bad, because everything that calls `/admin/*` or `/metrics` must be inside the list, or it gets 403. That includes
  Prometheus, scripts, Home Assistant REST sensors, and `deploy.py`, which asks `/admin/state` over loopback on the
  router host.
* Neutral: an existing installation with a network listener gets a new information finding until it sets the list.

### Confirmation

`test/selftest_admin_networks.py` runs in CI. It checks the following.

**Middleware:**

* Without networks, any address reaches the login, as before.
* With networks, an address outside is refused with 403 and reaches no handler. This holds:
  * on `/`, the UI files, `/admin/*`, `/metrics` and a write;
  * with valid credentials;
  * for an IPv4-mapped address outside, an unknown address or no address.

  No password is checked and no failure slot is used.
* An address inside, IPv4-mapped included, goes on to the login.
* The unauthenticated loopback switch does not open the admin part.
* `/v1/` paths are not affected.

**Control app:**

* The `/v1/` routes are exactly the three agent paths.
* `/v1/` paths with dot segments or encoded slashes resolve to 404, never to an admin handler.

**Configuration:**

* An absent key gives an empty list.
* Invalid lists are refused.
* A loopback listener without a loopback network is refused.
* A network listener needs no loopback network.
* The web UI's settings path refuses the key.

**Posture:**

* The finding appears for a network listener without the list.
* It does not appear with the list, or with a loopback listener.

`test/selftest_posture.py` and `test/selftest_security.py` still pass.
