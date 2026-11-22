---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Busy detection is a set of signal sources that can be switched off

> Part of the portability roadmap, phase 2 ([design/portability.md](../../design/portability.md)). Refines
> [ADR-0004](0004-busy-detection-is-a-first-class-scheduler-input.md) (busy detection is a first-class scheduler input);
> keeps [ADR-0019](0019-gpu-guard-power-limit-on-by-default.md) (GPU guard) and
> [ADR-0022](0022-components-report-state-as-facts.md) (facts). Follows the phase 0 switch for game detection (its own
> PR) and the pure node assessment of phase 1 (its own PR and ADR). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

Skirnir was built for GPU nodes that are also gaming PCs. Whether a node is busy is decided in `evaluate`
(`router/skirnir_router/poll.py`, from line 177) from several signals at once: GPU utilisation held above a threshold
together with foreign VRAM (`util_hot`, `hot_long`), foreign VRAM above a threshold for a while (`foreign`), a running
game that needs the GPU (`game_since`, `game_heavy`), and, as separate brakes, memory pressure (`Node.mem_pressure`,
`router/skirnir_router/nodes.py`, line 203) and the GPU guard state used by the scheduler
(`router/skirnir_router/scheduler.py`, around line 94). The thresholds are configurable (`modes.busy_enter`,
`router/skirnir_router/config.py`, from line 517), but the signals themselves are not.

`evaluate` decides and acts in the same function: on a transition it starts `unload_big_models` and `prewarm` itself
(`poll.py`, around lines 230-257), and it also catches up a prewarm after a game. That mixes policy with effects and makes
the state machine hard to test without its side effects. It is also the most tuned code in the router: each threshold and
hold time has a measured incident behind it (see the comments in `evaluate` and ADR-0095 to ADR-0097).

On the agent, game detection always runs (`newGameWatch` in `agent/app.go`, line 63); on Linux it reports
`supported: false` (`agent/game_other.go`). A dedicated inference server has no games and no desktop user whose foreign
VRAM should push the router away, yet it cannot switch these signals off; foreign VRAM from another service on a server is
a different case from a game on a desktop.

## Decision Drivers

* A dedicated server must be able to run without the gaming-PC logic, with no hidden effect left.
* The tuned behaviour on gaming PCs must not change: the defaults are today's behaviour.
* Policy and effects separated, so the state machine can be tested on its own.
* Each signal reported as a fact with its source (ADR-0022).

## Considered Options

* Busy signal sources that can each be switched off, an event-returning state machine and a dispatcher that acts.
* A single per-node switch "dedicated server" that skips busy detection.
* Keep the code and only expose more thresholds.

## Decision Outcome

Chosen option: "Signal sources, events and a dispatcher", because it lets each node use exactly the signals that make
sense for it and makes the state machine testable, without retuning anything.

* Signal sources: `foreign_vram`, `gpu_util`, `game`, `memory_pressure`, `gpu_guard`; each can be enabled per node
  (policy) or globally, all on by default as today.
* First step, before any signal becomes switchable: `evaluate` returns events (`BecameBusy(reason)`, `BecameFree`,
  `PrewarmDue(reason)`, `UnloadDue`) instead of spawning tasks; a dispatcher executes them with today's conditions
  (`unload_on_busy`, `quiet`, `prewarm_on_free`). Behaviour stays identical; the existing tests are the net.
* Second step: the signal switches. A switched-off source neither makes the node busy nor blocks a prewarm.
* The agent's game detection uses the phase 0 switch; the router treats a missing game report as "no signal", not as
  "free".

### Consequences

* Good, because a server node can drop the gaming logic completely and is assessed only by load and memory.
* Good, because the state machine becomes a pure function of node state and time, testable without side effects.
* Bad, because this is the riskiest step of the roadmap: the most tuned code, and every regression is a game that
  stutters or a model that does not get loaded.
* Bad, because more switches mean more combinations to support.

### Confirmation

The existing self-tests around busy and prewarm (for example `test/selftest_game_prewarm.py`,
`test/selftest_fallback_keeps_busy_model.py`) and the end-to-end suite pass unchanged after the event refactoring, before
any switch exists. New self-tests feed `evaluate` sequences of heartbeats and assert the returned events; a test per
switched-off source shows it no longer changes the state. The default configuration produces the same `busy` and `free`
journal events as before.
