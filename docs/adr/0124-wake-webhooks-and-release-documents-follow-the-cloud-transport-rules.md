---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Wake webhooks and release documents follow the cloud transport rules

> Refines ADR-0117 (waking nodes and fetching Ollama releases go through replaceable sources, proposed in PR #177), which
> leaves the egress and secret handling of the new outbound destinations open ("the same egress and secret handling as
> other outbound calls"). Applies [ADR-0048](0048-restrict-cloud-provider-transport.md) (final HTTPS destinations for cloud
> providers) and keeps [ADR-0021](0021-ollama-as-agent-child-updated-through-the-router.md) (the router chooses the
> version, never the code). Implemented in router 0.41.0.

## Context and Problem Statement

ADR-0117 adds two outbound destinations to the router: the `http_webhook` waker, which calls a URL with an optional bearer
token to switch a machine on, and the `document` release source, which reads the latest Ollama version and its hashes from
a URL. Both URLs come from the operator; the router reaches them from inside the house network, so a loose rule would turn
them into a request forwarder (redirects into the LAN, query strings with secrets in logs, an unauthenticated call when a
secret is missing) or into a path for code (a document that points the agent at another archive). Waking also has a
safety side: a webhook that pulses a power button switches the machine off again if it is repeated.

## Decision Drivers

* No new trust boundary that is weaker than the cloud path (ADR-0048), and one rule set for every configured destination.
* The web UI must not be able to point the router at a new destination or secret (trust base only in the configuration
  file, ADR-0016).
* A missing secret is a fact that stops the call, not a reason to call without it (ADR-0022).
* The update trust model of ADR-0021 stays: the agent verifies against its own source.
* No command execution by the unprivileged router.

## Considered Options

* ADR-0048's transport rules for both destinations; destinations only in the configuration file; strict document shape.
* Allow queries, redirects and plain HTTP in the LAN for convenience (smart plugs such as `http://<plug>/relay/0?turn=on`).
* A `command` waker (`ipmitool`, `etherwake`, a script) and a native `ipmi` waker with credentials in the router.

## Decision Outcome

Chosen option: "ADR-0048's transport rules, configuration-file destinations, strict document", because it reuses a reviewed
boundary instead of inventing a second one and keeps every new destination visible in one reviewed file.

* **Transport** (shared helper `values.egress_url`): the configured URL is final; HTTPS, plain HTTP only to literal
  `127.0.0.1`/`::1`; no credentials, query or fragment; a 3xx answer is refused, not followed; a total timeout (webhook
  `timeout_s` 1-60 s, default 10; document 20 s). The configured URL is the allowlist entry: wakers and release documents do
  not use `router.cloud.egress_allow`, which governs where classified request data may go.
* **Destinations and secrets only in `config.yaml`:** `router.wakers.<name>` holds `url` and `token_env` (an environment
  variable name, never the token). Nodes choose a waker by name (`nodes.<name>.waker` or the policy key `waker`); the policy
  API accepts only `wol` or a configured name. An unknown name is refused at load; a name that disappears later makes the node
  not wakeable (`wake_unknown_waker`) instead of falling back silently.
* **Fixed body, fail closed:** the webhook body is always `{"event": "wake", "node", "mac"}`, nothing from a request goes
  into it. If `token_env` is set and the variable is empty, nothing is sent (`wake_secret_missing`). The log names the
  receiver's host only, never the URL path (a Home Assistant webhook ID is a secret). Every failure is a `Problem` with a fixed
  `kind`; the router does not wait after one.
* **One signal per attempt for webhooks:** `repeat` defaults to `false`; `wol` keeps re-sending every
  `retry_interval_s` as before.
* **Release document shape:** at most 64 KiB and 32 entries, `version` in the agent's version grammar, `sha256` 64 hex
  digits, `size` an integer byte count, and each `url` must end in the archive name the agent expects for that platform;
  unknown platforms are skipped, a malformed entry for a known platform discards the document. The router still sends only
  version, archive name, hash and size; the agent fetches from its own `ollama_update.source` and refuses a hash that does
  not match that source's `sha256sum.txt`, so no agent change is needed and a document cannot supply code.
* **The GitHub source is unchanged**, including following redirects: GitHub serves `sha256sum.txt` through a redirect to
  its download host, and the host pair is fixed by default rather than chosen per installation.
* **No command waker, no native IPMI yet:** the router runs as its own unprivileged user with `NoNewPrivileges` and executes nothing
  the configuration names (ADR-0118). BMC (IPMI/Redfish) or cloud power-on goes through a webhook receiver that holds those
  credentials; a native `ipmi` adapter would be a separate change with its own review of credential handling.

### Consequences

* Good, because both new destinations inherit a reviewed rule set and its tests, and the UI cannot widen them.
* Good, because a mirror for Ollama needs no agent release, and the agent's hash check stays the last word.
* Bad, because plain-HTTP smart plugs on the LAN cannot be called directly; they need a loopback relay or a home-automation
  webhook in between.
* Bad, because IPMI users need a receiver of their own until a native adapter exists.

### Confirmation

`test/selftest_wakers.py`: the Wake-on-LAN packet, cadence, eligibility and cooldown as before; the webhook against a
loopback receiver with fixed body and token, missing token (nothing sent), redirects 301-308 (target never contacted),
refusal, a timeout while other coroutines keep running, an unreachable receiver; configuration and policy refusals (plain
HTTP to a non-loopback host, `localhost`, query, fragment, credentials, `command`/`ipmi` kinds, unknown names).
`test/selftest_release_source.py`: the document yields the same entry as the GitHub source for the same release; redirect,
oversized (with and without Content-Length), timeout, HTTP error, invalid JSON and every malformed field are refused. The
agent's existing `TestOllamaUpdateRun` keeps refusing an order whose hash differs from its source's `sha256sum.txt`.
