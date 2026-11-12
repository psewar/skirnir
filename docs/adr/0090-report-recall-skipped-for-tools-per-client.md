---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# Report recalls skipped for tools as a state per client

> Source: a review of how clients with automatic recall behave once they offer tools. Complements ADR-0059.

## Context and Problem Statement

ADR-0059 skips automatic recall when a request carries a nonempty `tools` list, unless the project grant sets
`recall_into_tools: true`. Each such request says so: `X-Skirnir-Memory-State: skipped:tools`, the journal entry, and
`skirnir_memory_events_total{operation="recall",outcome="skipped_tools"}`.

None of these reaches the operator when it matters. A client configured to receive recalled knowledge, for example a
Home Assistant conversation entry, keeps working after someone enables a tool API on it; from then on every request
carries tools and gets no facts. The client does not read the header, the journal shows one line per request among all
others, and the counter has no client, so it cannot say which client lost its knowledge or whether that client still
gets any. The answers only become worse.

The skip itself is intended. What is missing is a signal that names the client and says that the state persists.

## Decision Drivers

* The router knows which client's recalls it skipped; it reports that as a fact instead of leaving the operator to
  infer it from many per-request headers (repository rule "report state as a fact").
* Memory metrics carry fixed labels only, never clients, sessions, banks or content
  (`design/shared-memory-interoperability.md`, Operations), and memory logs name no principal and no project
  (`design/hindsight-memory.md`). The clients are named only behind admin authentication, where the journal already
  shows each request's client and memory state.
* Behaviour must not change: no request is refused or altered, and nothing needs configuring.
* A client that sends tools on every request must not write a log line per request.

## Considered Options

1. A state per client: the current run of recalls skipped for tools, in `/admin/context` and the Overview, an aggregate
   gauge, and one rate-limited warning line without names when a run starts
2. A counter with a `client` label (`skirnir_memory_recall_skipped_total{client,reason}`)
3. A posture finding (ADR-0064) for clients with recall configured
4. A warning line per skipped request, naming the client

## Decision Outcome

Chosen option (accepted by the operator 2026-11-12): 1, because it is the smallest signal that names the client, says
that the state persists, and alerts without labelling memory metrics with clients.

* A **run** starts with a client's first recall skipped for tools and ends with its next recall that passes the tools
  check (no tools, or the grant opts in). It counts its requests and keeps the project of the last one. Explicit
  `routing.memory` recall and the strategies `auto`/`hybrid` are treated alike: both asked for recall and got none.
* `/admin/context` lists the runs under `recall_skipped_for_tools`: `{client: {project, since, last, requests}}`
  (epoch seconds), while the last skipped request is at most an hour old. A client that stops asking for recall, or is
  reconfigured without recall, drops out on its own. The Overview shows them in a tile.
* `skirnir_memory_recall_skipped_for_tools_clients` is the number of such clients, without a client label; an alert on
  `> 0` points the operator to the Overview or `/admin/context`.
* The first request of a run logs one warning with the number of clients in this state, where they are named and the
  next step (opt in with `recall_into_tools: true`, which accepts the ADR-0059 risk, or nothing to do). It names no
  client and no project. At most one line per client and hour, however often its requests switch between tools and no
  tools.
* The state lives in memory: a restart starts over, and the next skipped request reports the client again.

Option 2 puts client names on a memory metric, against the documented rule, and a counter alone does not say whether a
client still gets any recall. Option 3 cannot know: whether a client sends tools is not in the configuration. Option 4
names the client in the log, against the documented rule, and writes the same line for every request of a client that
always sends tools.

### Consequences

* Good, because a client that lost its knowledge to the tools skip is named in the Overview and in `/admin/context`, and
  the log says that it happened and where to look, with the step to take.
* Good, because one gauge without client names is enough to alert on it.
* Good, because nothing changes for requests and nothing needs configuring.
* Bad, because the state is lost on restart; until the client's next tool request it is not reported.
* Bad, because the log line alone does not say which client; the operator has to open the Overview or `/admin/context`.
* Neutral, because a client that is meant to work with tools and without recall is listed too; the warning appears at
  most once an hour, and the operator can turn the client's recall off or opt it in.

### Confirmation

`test/selftest_memory.py`: `test_a_client_whose_recalls_are_skipped_for_tools_is_reported_as_a_state` (a `hybrid` client
sending tools appears in `/admin/context` with project and request count, and without the control login the route
answers 401 without naming it; the gauge counts clients without naming them,
one warning line per run without client, project or request content, a recall without tools or the grant's opt-in ends
the run) and
`RecallSkipRunTests` (the window, a new run after it, the hourly limit on the warning, one run per client).
