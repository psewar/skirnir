---
status: accepted
date: 2026-10-24
decision-makers: operator
---

# Switching to the standby waits for a gap on the server

> Recorded retroactively on 2026-10-30; decided on 2026-10-24 (v0.23.4, router 0.16.4). Source: CHANGELOG v0.23.4,
> PR #64, README "Switching to the standby for a request".

## Context and Problem Statement

The hold stops the OpenAI-compatible server at once. A request for a standby model (a memory write's extraction call)
held the server while an overnight agent's request was streaming from it; the stream was cut off after 5.8 s. In the other
direction the agent already lets the standby's answers finish before it stops the standby.

## Decision Drivers

* Running answers must never be cut off by a switch.
* Two requests waiting to switch must not wait for each other.
* A request that cannot get a gap needs a clear answer.

## Considered Options

* The switching request waits until no answer runs on the server (`Node.server_inflight`, not counting requests waiting
  to switch), at most for its priority's admission budget; `ensure_standby` checks again right before the hold; without a
  gap 503 with the reason code `server_busy` and `Retry-After` 30 s
* Exclude the node while the server answers (the request falls to the next tier or fails at once)
* Let the agent delay the hold until the server is idle

## Decision Outcome

Chosen option: "Wait for a gap in the router", because the router knows the running requests and the waiting request's
deadline, and an agent's tool calls leave gaps between its requests. The refusal code follows ADR-0024.

### Consequences

* Good, because no stream is cut off by a switch any more.
* Bad, because a waiting request can be delayed up to its admission budget, and a busy server can be preempted in every
  gap (the switch itself still costs about a minute).

### Confirmation

`test/selftest_switch_waits_for_gap.py`; a mutation probe without the wait in `ensure_standby` fails both hold cases.
