---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# One pure node assessment for selection and explanation

> Refines how the scheduler applies ADR-0022 (components report state as facts) and the breaker of stage 3. No routing
> rule changes. Source: portability review of the router core (doubled feasibility logic, policy mixed with effects).

## Context and Problem Statement

Whether a node can serve a tier was decided in two places. `scheduler.candidates_for` filtered the nodes a request may
use; `scheduler._node_block` ("Like candidates_for") produced the base code and the numbers a client gets with a 503
(`gpu_busy`, `vram_full`, `memory_pressure`, ...). Every rule added since (standby switch 0.15.0, release of the held
server 0.16.3, the agent's RAM block 0.17.0, memory pressure) had to be written twice, in two different orders. They
already differed in one corner: with the held server's model loaded while the standby Ollama serves, the selection
refused the node under memory pressure or an agent block, while the explanation said it could serve, so the client got
`no_node` without a reason.

The selection was also not pure. `candidates_for(mutate=True)` moved an expired open breaker to `half_open` and wrote a
journal entry while judging; views passed `mutate=False` to avoid it. The VRAM budgets repeated the same arithmetic: the
prewarm plan and the standby switch both computed "card minus reserve, desktop baseline and foreign VRAM", the residency
check and the too-large test "card minus the free reserve", and the measured occupancy of loaded models was summed twice.

## Decision Drivers

* No behaviour change: the most tuned code in the router, every rule there has an incident behind it.
* One rule, one place: selection and explanation must not be able to drift apart again.
* A selection that only asks must not change state (views, tests, a future simulation over a snapshot).
* Small enough to review line by line.

## Considered Options

* **One function `assess(node, tier, ctx, now) -> (code | None, facts)`, a pure selection, and the breaker step in the
  caller.**
* Keep both functions and add a test that they agree. Rejected as the only measure: the test catches drift after the
  fact, the next rule is still written twice.
* A full snapshot-based scheduler (`choose()` over an immutable copy of all nodes). Deferred: larger, and it touches the
  proxy's wait and retry logic; the pure `assess` is a step towards it.
* Leave the breaker transition inside the selection. Rejected: it is the selection's only side effect, and moving it out
  can be done exactly (below).

## Decision Outcome

Chosen option: one pure assessment.

* `scheduler.assess(n, t, ctx, now)` judges a local node that offers the tier's model, in this order: state gate
  (offline, draining, busy for a tier without `busy_ok`), breaker (`breaker_would_allow`), then by how the model is
  reached: release of the held server (memory pressure or the agent's RAM block refuse), switch to the standby
  (memory pressure, then the standby budget), already loaded with enough context (no budget), memory pressure, cold load
  against `budget_gib`. A failed fit reports `model_too_large`, `gpu_busy` (foreign VRAM) or `vram_full` with the numbers.
  This is the order `candidates_for` used, with the codes `_node_block` produced.
* `candidates_for` keeps the nodes `assess` finds nothing against; `blockers()`/`unavailable()` explain the others. Its
  return value is `(ctx, candidates)`; the third value (the requirement of whichever node was judged last) had no reader.
* The selection (`candidates_for`, `choose`) changes no node; the `mutate` parameter is gone. The request path
  (`proxy._Acquire`) and a shadow run (`ops.run_shadow`) call `scheduler.probe_breakers(tiers, now, exclude, req)` right
  before they select. It moves exactly the breakers the selection reaches (tiers the request does not block, targets not
  excluded, nodes offering the model and passing the state gate) from expired `open` to `half_open`, with log and journal
  entry, so the probed target is ranked with its probe penalty as before. Views (Home Assistant, `/metrics`,
  availability, fallback observation) do not call it, as they passed `mutate=False` before. `Breaker.breaker_allows` became
  `Breaker.breaker_probe`; `breaker_would_allow` already let an expired open breaker through, so the verdict is the same
  with or without the probe.
* `role_possible_when_free` shares the empty-card test with `assess` (`model_too_large`); the rest of `assess` does not
  apply there, since busy, draining and the breaker are what that question looks past.
* Budgets: `nodes.card_gib(node, reserve_for)` is the empty card minus a state's reserve (used by `budget_gib` without a
  fresh heartbeat, the residency room and the too-large test); `Node.card_budget_gib(reserve_for)` subtracts baseline and
  foreign VRAM (standby budget with the node's own reserve, prewarm plan with the free reserve). The residency room keeps
  its coarser formula without baseline and foreign VRAM, as before. `Node._real_gib` is the measured occupancy of one
  loaded model, summed by `ollama_vram_gib` and `answering_vram_gib` in the same order as before (a plain loop: `sum()`
  of floats rounds differently since Python 3.12). `loading_for` and `tier_loading` share one loop.

### Consequences

* Good: a new rule about whether a node can serve is written once and shows up in both the selection and the 503 reason.
* Good: selection can be tested and called from views without side effects; the side effect is one named call.
* Neutral: in the corner above (server's model loaded while the standby serves, under memory pressure or an agent block)
  the explanation now says `memory_pressure`, matching the selection; before it said nothing. The agent does not report
  this state (the held server's model is never loaded while the standby serves), so this is not seen in operation.
* Bad: a caller that selects for a request must remember `probe_breakers`; without it, an expired breaker is still let
  through (same verdict) but is not marked `half_open` and is ranked without the probe penalty until a request path
  probes it. Both request paths do it; the self-test asserts the selection itself stays pure.

### Confirmation

* `test/selftest_node_assessment.py` walks 2112 node states (offline, free, busy with a game or GPU load, draining, six
  breaker states, VRAM fit, no fit, foreign VRAM, too large, warm, warm with too small a context, standby switch with and
  without room, release with and without the agent's RAM block, the corner, memory pressure) and asserts that
  `candidates_for`, `assess`, `blockers` and `choose` agree, that the selection changes no breaker and writes no journal,
  and that `probe_breakers` moves exactly the due breakers past the state gate, for nodes and a cloud provider.
* Before the merge, the refactor was compared with the previous code on 20,000 random node states (judgement, candidates,
  blockers, loading waits, `choose` on the request path with breaker states, journal and routing record, the shadow path,
  all budgets): identical except the corner above, where only the explanation changed. All self-tests in CI and the
  end-to-end suite (breaker open, half-open probe, closed) pass unchanged in substance; the existing tests only follow the
  rename `_node_block` -> `assess` and the two-value return of `candidates_for`.
