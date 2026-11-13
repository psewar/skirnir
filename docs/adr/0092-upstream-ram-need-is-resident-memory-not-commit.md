---
status: proposed
date: 2026-11-13
decision-makers: operator
---

# The upstream's RAM need is its resident memory, not its commit

> Source: a measurement on a Windows node with 64 GiB RAM, 2026-11-13, after Strata 0.1.59 gained a resident RAM budget.
> Refines ADR-0037: replaces its measure of the RAM need (peak private bytes of the last run). The held start, the RAM
> check before a release and `max_ram_percent` stay as decided there.

## Context and Problem Statement

ADR-0037 has the agent learn the RAM need of its held OpenAI-compatible server as the peak **private bytes** (commit) of
the server's process tree in its last run, stored as `need_bytes` in `<identity dir>/upstream.json`. Before every
release the agent requires `RAM in use + need <= max_ram_percent`. The reasoning then: the server commits its experts
at load and pages them in over hours, partly locked for the GPU, so the working set understates the eventual footprint.

With all experts in RAM the agent learned a need of 41 GiB. With a resident RAM budget (`--resident-budget-gib 16`) the
server process had a working set of 28 GiB and private bytes of 52 GiB, and the agent learned 52.3 GiB. The budget
lowered the RAM the server occupies, yet the learned need rose: commit counts memory that is reserved but not resident,
such as CUDA host allocations and views of expert files. With 52.3 GiB on a 64 GiB machine and a limit of
95 %, the check lets the server start only at 8 GiB or less in use, which in practice is never. The measure has to
follow what a start takes from the RAM that is available, which is what the check compares with.

## Decision Drivers

* The need predicts how far a start lowers the available RAM (the quantity `ramBlock` compares), not what the server
  reserves.
* Pages a driver holds for the server outside its working set (pinned for the GPU and trimmed from the working set, or
  allocated by the graphics kernel on its behalf) must count, or the check would let a start through that does not fit.
* Underestimating is the dangerous side (the check exists because a start at 98 % RAM ended in a bluescreen),
  overestimating the costly one (the server never starts).
* The agent derives the need itself (no configured number that goes stale), and reports what it measured as facts.
* Linux keeps a sensible behaviour without per-process measurement there.

## Considered Options

* Keep the peak private bytes (commit)
* Peak private working set of the tree (`PROCESS_MEMORY_COUNTERS_EX2.PrivateWorkingSetSize`, or the performance counter
  `Process\Working Set - Private`)
* Peak working set of the tree, together with the RAM that becomes available when the server stops, plus a margin; the
  need only grows while the server's start configuration stays the same
* The system-wide rise of RAM in use between the release and the run's peak
* A need the operator configures

## Decision Outcome

Chosen option: "Peak working set together with the RAM released at the stop", because it is the only option that sees
both what the server holds in its working set and what a driver holds for it outside, without counting reservations.

* **Resident peak**: the sampler (every 30 s while the server runs, and once more before a hold) sums the working set
  (`WorkingSetSize` of `K32GetProcessMemoryInfo`) over the server's job. The working set contains private pages and
  resident file-backed pages, such as mapped model files; those are not available to anyone else while resident either,
  which is why the private working set alone is not enough. Pages two processes of the tree share count twice, on the
  safe side.
* **Released at the stop**: before a hold stops the server the agent reads the available RAM, and again right after the
  process tree has ended and once more after two seconds (the system returns a dead process's pages, and a driver its
  pinned pages, shortly after). The larger difference is what the tree held in all, inside its working set or not. It is
  measured before the standby starts. Other programs changing their memory at that moment blur it; a game starting
  lowers it, which the resident peak covers.
* **Need** = the larger of both, plus 10 %. The margin covers what a 30-s sampler misses (peaks while loading) and that
  the release shows the end of a run, not its top.
* **Only upwards**: resident memory grows over hours as experts are paged in, and the router often holds the server
  seconds after it answers. A short run would otherwise lower the need below what a long run takes. The need is learned
  again only when the server's start configuration changes (a fingerprint of command, working folder, arguments and
  environment, `config` in the file): a smaller setup is usually a changed argument such as the budget. A change the
  configuration does not show (a model swapped behind the same command line) keeps the higher need; deleting
  `upstream.json` relearns it.
* **Migration**: a need stored by an older agent (no `measure` in the file) was commit and is discarded; the models are
  kept, so the agent still starts held. Until the next run has been measured the release has no RAM check, as on the
  first start since ADR-0037.
* **Facts**: `upstream.json` records `measure: "resident"`, `config`, `peak_resident_bytes`, `released_bytes` and, for
  comparison only, `peak_commit_bytes`; the hold logs all of them with the resulting need.
* **Linux**: the supervisor lists the process tree only on Windows, so there is no resident peak; the RAM released at the
  stop (`MemAvailable`) still gives a need. Before, a Linux node never learned one and never checked.

### Consequences

* Good, because the need follows a resident budget: with 16 GiB and a release no larger than the working set, the need is
  about 31 GiB instead of 52.3, so the server starts with up to about 29 GiB in use at a 95 % limit on 64 GiB.
* Good, because memory pinned for the GPU outside the working set is still counted, through the release, and the file
  shows which of the two measures set the need.
* Good, because a short run no longer lowers the need, which commit hid (commit was at its full height right after load).
* Bad, because the first run after this update, and after a change of the start configuration, has no RAM check, and a
  first short run learns a need below the eventual one until a longer run raised it; the margin narrows the gap only.
* Bad, because the release measure is blurred by other programs that change their memory in the same seconds, and a hold
  takes up to two seconds longer before the standby starts.
* Bad, because a smaller setup that the configuration does not show keeps the old, higher need until `upstream.json` is
  deleted.
* Neutral, because a Linux node with a standby and `max_ram_percent` now learns a need and may refuse a release where it
  never did; that is what the setting asks for.

### Confirmation

* Agent tests: `procmem_windows_test.go` (real call: committed but untouched memory raises commit, not the resident
  measure; touching it raises both); `upstream_start_held_test.go`: the sampler learns the resident peak plus margin and
  not the commit, a rise below 256 MiB is kept but not written until the hold, a shorter run does not lower the need, the
  hold learns the RAM released at the stop (larger than the resident peak) without any sample, an older agent's commit
  need is discarded with the models kept, a changed start configuration relearns, `childConfig` sees every part of the
  start configuration.
* Node check: after one run of the server under the budget, `upstream.json` shows `measure: "resident"`,
  `peak_resident_bytes` close to the server's working-set peak in Task Manager, `released_bytes` close to the rise of
  available RAM when it stopped, and `need_bytes` about 1.1 times the larger of them, far below `peak_commit_bytes`. If
  `released_bytes` is much larger than `peak_resident_bytes`, the server holds pinned memory outside its working set and
  the need rightly follows the release.

## Pros and Cons of the Options

### Keep the peak private bytes (commit)

* Good, because it is at its full height right after load, so even a short run learns the whole need.
* Bad, because it counts reservations that never become resident; with a resident budget it rose while the RAM taken fell,
  and the server could no longer start.

### Peak private working set

* Good, because it ignores reservations and shared library pages.
* Bad, because it misses resident file-backed pages (mapped model files), which are not available while resident, and
  pages a driver holds outside the working set; it would underestimate, the dangerous side.

### Peak working set together with the RAM released at the stop (chosen)

* Good, because together they see the working set and the memory held outside it, and the stop is a moment where the
  server's share of the RAM shows directly.
* Bad, because the release is measured only at a hold and is blurred by other programs; the working set counts shared
  pages, slightly on the safe side.

### System-wide rise of RAM in use during the run

* Good, because it is exactly the quantity the check compares.
* Bad, because a run lasts hours: games, browsers and the standby change the RAM meanwhile, so the rise says little about
  the server.

### A need the operator configures

* Good, because it is predictable.
* Bad, because the agent can measure it; a number in the configuration goes stale with every model or budget change, and
  a wrong one either blocks the server or lets a start through that does not fit.

## More Information

ADR-0037 (start held, RAM check before a release; this ADR replaces its measure), ADR-0035 (the router checks no budget
when it releases a held server; the agent's check is the only one), ADR-0022 (report state as facts). Code: `agent/procmem_windows.go` (`procMemory`), `agent/supervisor.go` (`childMemory`,
`childConfig`), `agent/upstream_hold.go` (`observeRun`, `releasedSince`, `endRun`), `agent/upstream_memo.go`.
