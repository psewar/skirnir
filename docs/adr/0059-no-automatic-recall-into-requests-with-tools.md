---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# No automatic recall into requests that carry tools

> Source: TM-10 in the threat model. Complements ADR-0060 (shared scopes name their writers).

## Context and Problem Statement

With the strategies `auto` and `hybrid` (and explicit `routing.memory`), the router inserts retrieved facts into the
prompt as an untrusted user-role data message. A fact in a shared scope may have been written by another client. When
the request also offers tools, a planted fact ("when asked about X, call tool Y with Z") reaches a model that can act;
the client executes the call. The untrusted-data notice asks the model not to follow instructions in facts, but that is a
request to the model, not an enforcement. Retention already refuses tool workflows (`Capture`); recall did not.

The router does not execute tools and cannot authorize individual calls. What it controls is which facts reach a
request that can lead to tool calls.

## Decision Drivers

* Facts from another writer must not steer a model that can call tools unless someone chose that explicitly.
* The decision belongs to the grant: the operator knows which client acts with tools and how it confirms them.
* A skipped recall is a state the client can see, never an empty result that looks like missing knowledge.

## Considered Options

* Skip automatic recall when the request carries a nonempty `tools` list, unless the project grant sets
  `recall_into_tools: true`
* Inject only facts written by the requesting client into tool requests (filter on provenance `origin_client`)
* Keep injecting and rely on the untrusted-data notice and client-side guardrails

## Decision Outcome

Chosen option: "Skip unless the grant opts in", because it is enforceable in the router, needs no trust in provenance
written by a backend, and leaves a deliberate exception visible in configuration. Filtering by provenance would depend on
metadata that consolidated observations lose and would still pass a client's own poisoned retains; relying on the notice
alone leaves the risk where it was.

* A request whose body carries a nonempty `tools` list (native, OpenAI and Anthropic shapes, after conversion) gets no
  automatic recall. The state is `skipped:tools` in `X-Skirnir-Memory-State` and the journal, counted as
  `skirnir_memory_events_total{operation="recall",outcome="skipped_tools"}`. Inference proceeds.
* A project grant may set `recall_into_tools: true` (boolean, requires `read`). Then recall is inserted as before, still
  as untrusted data. Opting in accepts the risk for that client; its tool calls should be confirmed or limited by the
  client.
* Explicit recall APIs (`/v1/skirnir/memory/recall`, the Hindsight gateway) are not affected: they return facts to the
  client, which decides what to do with them.

### Consequences

* Good, because a fact written by one client can no longer steer another client's tool use by default.
* Good, because the exception is a named, reviewable setting per grant.
* Bad, because a tool-using client loses automatic knowledge unless the operator opts it in.
* Neutral, because a client that opts in is protected only by the untrusted-data notice and its own confirmation of tool
  calls, as before.

### Confirmation

`test/selftest_memory.py`: `test_no_automatic_recall_into_a_request_with_tools_unless_the_grant_opts_in` (native, OpenAI
and Anthropic requests with tools get `skipped:tools` and no recall call; an empty list recalls; `true` restores recall,
a string does not; another client stays skipped) and `test_recall_into_tools_is_an_explicit_boolean_that_needs_read`.
