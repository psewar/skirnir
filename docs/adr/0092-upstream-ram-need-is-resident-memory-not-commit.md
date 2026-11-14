---
status: proposed
date: 2026-11-14
decision-makers: operator
---

# The upstream's RAM need is its resident memory, not its commit

> Source: measurements on a Windows node with 64 GiB RAM, 2026-11-13, after Strata 0.1.59 gained a resident RAM budget.
> Refines ADR-0037: replaces its measure of the RAM need (peak private bytes of the last run). The held start, the RAM
> check before a release and `max_ram_percent` stay as decided there.
> Revised on 2026-11-14 before acceptance (agent 0.35.1): the first version (agent 0.35.0, never rolled out) counted the
> whole working set, which still includes file pages the system takes back; the decision now counts private memory only.

## Context and Problem Statement

ADR-0037 has the agent learn the RAM need of its held OpenAI-compatible server as the peak **private bytes** (commit) of
the server's process tree in its last run, stored as `need_bytes` in `<identity dir>/upstream.json`. Before every
release the agent requires `RAM in use + need <= max_ram_percent`. The reasoning then: the server commits its experts
at load and pages them in over hours, partly locked for the GPU, so the working set understates the eventual footprint.

Measured with Strata's resident RAM budget (`--resident-budget-gib N`):

| | All experts in RAM | Budget 16 GiB | Budget 8 GiB |
|---|---|---|---|
| Need learned from commit | 41 GiB | 52.3 GiB | not measured |
| Working set after long prompts | | 28 GiB | 29 GiB |
| Memory pinned for the GPU (Task Manager, shared GPU memory) | | 19 GiB | 11 GiB |

The budget lowered the RAM the server needs, yet the learned need rose: commit counts memory that is reserved but not
resident, such as CUDA host allocations and views of expert files. With 52.3 GiB on a 64 GiB machine and a limit of
95 %, the check lets the server start only at 8 GiB or less in use, which in practice is never.

The working set is no better guide under a budget: with 8 GiB it was as large as with 16, because the parts of the
expert file the server read stay in its working set as file pages. Strata itself warns about this for memory-mapped
experts. The system moves such pages to the standby list (available RAM) when memory gets tight, so they are not RAM a
start takes for good. The operator wants the server with a small budget to run next to other workloads; a need that
counts these pages would keep it from starting there.

## Decision Drivers

* The need predicts the RAM a start takes for good: memory the system can get back only by paging it out.
* Pages a driver holds for the server outside its working set (pinned for the GPU and trimmed from the working set, or
  allocated by the graphics kernel on its behalf) must count, or the check would let a start through that does not fit.
* Underestimating is the dangerous side (the check exists because a start at 98 % RAM ended in a bluescreen),
  overestimating the costly one (the server never starts, and a resident budget buys nothing).
* The agent derives the need itself (no configured number that goes stale), and reports what it measured as facts.
* Linux keeps a sensible behaviour without per-process measurement there.

## Considered Options

* Keep the peak private bytes (commit)
* Peak working set of the tree, together with the RAM that became available at the stop (agent 0.35.0, first version
  of this ADR)
* Peak private working set of the tree, together with the RAM that became available at the stop without what went to
  the file cache, plus a margin; the need only grows while the server's start configuration stays the same
* The system-wide rise of RAM in use between the release and the run's peak
* A need the operator configures

## Decision Outcome

Chosen option: "Peak private working set together with the RAM released at the stop without the file cache", because it
counts what the server holds for itself, inside its working set and outside it, and nothing the system can take back or
never handed out.

* **Private peak**: the sampler (every 30 s while the server runs, and once more before a hold) sums the private
  working set (`PrivateWorkingSetSize` of `PROCESS_MEMORY_COUNTERS_EX2`, `K32GetProcessMemoryInfo`) over the server's job.
  Pages the process locked in its working set (pinned for the GPU) are private and count. Read file pages, shared
  library pages and reservations do not. A system without the EX2 counters falls back to the whole working set, on the
  safe side.
* **Released at the stop**: before a hold stops the server the agent reads the available RAM and the system cache
  (`GetPerformanceInfo`: standby list plus system working set), and again right after the process tree has ended and
  once more after two seconds. Released = the rise of available RAM minus the growth of the cache, i.e. the memory the
  tree held for itself, including pages a driver held outside its working set. The larger of the two reads counts. It is
  measured before the standby starts. Other programs changing their memory at that moment blur it; a game starting
  lowers it, which the private peak covers.
* **Need** = the larger of both, plus 10 %. The margin covers what a 30-s sampler misses (peaks while loading) and that
  the release shows the end of a run, not its top.
* **Only upwards**: memory grows over hours as the server works, and the router often holds the server seconds after it
  answers. A short run would otherwise lower the need below what a long run takes. The need is learned again only when
  the server's start configuration changes (a fingerprint of command, working folder, arguments and environment,
  `config` in the file): a smaller setup is usually a changed argument such as the budget. A change the configuration
  does not show (a model swapped behind the same command line) keeps the higher need; deleting `upstream.json` relearns
  it.
* **Migration**: a need measured another way is discarded: commit (no `measure` in the file, agents up to 0.34.x) and
  the whole working set (`measure: "resident"`, agent 0.35.0). The models are kept, so the agent still starts held.
  Until the next run has been measured the release has no RAM check, as on the first start since ADR-0037.
* **Facts**: `upstream.json` records `measure: "private"`, `config`, `peak_private_working_set_bytes`,
  `released_bytes` and, for comparison only, `peak_working_set_bytes`, `released_to_cache_bytes` and
  `peak_commit_bytes`; the hold logs all of them with the resulting need.
* **Linux**: the supervisor lists the process tree only on Windows, so there is no private peak; the rise of
  `MemAvailable` at the stop gives the need. It rises by the anonymous memory freed; the page cache the server read stays
  cached and was counted as reclaimable before and after. Before, a Linux node never learned a need and never checked.

### Consequences

* Good, because the need follows a resident budget: the budget's private memory counts, the expert pages read through
  the file cache do not, so a small budget lets the server start next to other workloads.
* Good, because memory pinned for the GPU still counts: locked in the working set as private memory, or outside it
  through the release.
* Good, because a short run no longer lowers the need, which commit hid (commit was at its full height right after load).
* Bad, because RAM in use, as the operating system and the check see it, can rise above `max_ram_percent` while the
  server reads its expert file: those pages count as in use until the system trims them. The check bounds the memory
  that cannot be taken back, not the RAM shown in use.
* Bad, because the first run after this update, and after a change of the start configuration, has no RAM check, and a
  first short run learns a need below the eventual one until a longer run raised it; the margin narrows the gap only.
* Bad, because the release measure is blurred by other programs that change their memory in the same seconds, and a hold
  takes up to two seconds longer before the standby starts.
* Bad, because a smaller setup that the configuration does not show keeps the old, higher need until `upstream.json` is
  deleted.
* Neutral, because a Linux node with a standby and `max_ram_percent` now learns a need and may refuse a release where it
  never did; that is what the setting asks for.

### Confirmation

* Agent tests: `procmem_windows_test.go` with the real calls: committed but untouched memory raises the commit, not
  the working sets; touching it raises both; reading a mapped file raises the working set but not the private working
  set; the system cache is readable. `upstream_start_held_test.go`:
  * the sampler learns the private peak plus margin, not the working set or the commit;
  * a rise below 256 MiB is kept but not written until the hold, and a shorter run does not lower the need;
  * the hold learns the RAM released at the stop without the part that went to the cache, without any sample;
  * `releasedSince` counts the whole rise without a cache reading, and nothing when RAM falls;
  * needs measured as commit or as the whole working set are discarded with the models kept, and a changed start
    configuration relearns;
  * `childConfig` sees every part of the start configuration.
* Node check: after one run of the server under a budget, `upstream.json` shows `measure: "private"`, and the agent
  log line `run of <child> ended` gives the private peak, the release with the part that went to the cache, and the
  resulting need, below the whole working set and far below the commit. If the release is much larger than the private
  peak, the server holds pinned memory outside its working set and the need rightly follows the release.

## Pros and Cons of the Options

### Keep the peak private bytes (commit)

* Good, because it is at its full height right after load, so even a short run learns the whole need.
* Bad, because it counts reservations that never become resident; with a resident budget it rose while the RAM taken fell,
  and the server could no longer start.

### Peak working set together with the RAM released at the stop (agent 0.35.0)

* Good, because it ignores reservations, and the release sees pages outside the working set.
* Bad, because it counts the file pages the server read, which the system takes back under pressure; with a budget of 8
  it would have learned about the same need as with 16, and a small budget could not run next to other workloads.

### Peak private working set together with the RAM released at the stop without the file cache (chosen)

* Good, because it counts exactly the memory the server holds for itself, inside and outside its working set.
* Bad, because RAM in use can rise above the limit through file pages, and the release is blurred by other programs.

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
when it releases a held server; the agent's check is the only one), ADR-0022 (report state as facts). Code:
`agent/procmem_windows.go` (`procMemory`, `systemCacheBytes`), `agent/supervisor.go` (`childMemory`, `childConfig`),
`agent/upstream_hold.go` (`observeRun`, `releasedSince`, `endRun`), `agent/upstream_memo.go`.
