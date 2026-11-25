---
status: proposed
date: 2026-11-25
decision-makers: maintainer (asked for by the model test bench, proposed by Claude)
---

# The agent reports Ollama's runner processes and their memory

> Builds on [ADR-0092](0092-upstream-ram-need-is-resident-memory-not-commit.md) (private working set as the
> RAM a process takes) and [ADR-0137](0137-the-router-uses-only-plausible-gpu-numbers-from-an-agent.md) (values outside
> the schema are dropped). The router's use of the fact for an unload is
> [ADR-0149](0149-an-admin-unload-answers-once-the-runner-memory-is-released.md).

## Context and Problem Statement

Ollama runs every loaded model in a runner process of its own (`ollama runner`, or `llama-server`), a child of the
server; under the agent both sit in the job object of the Ollama child. The runner holds the model's main memory. After
`keep_alive: 0` the model leaves `/api/ps` at once, but the runner keeps its memory for several seconds until it has
exited. The model test bench unloads and then starts a large job on the same node (a WSL VM); started in that gap, the job
pushed Windows to about 93 % RAM. The bench waits for an empty `/api/ps` and polls the node's RAM, which is a guess: free
RAM moves with everything else on the machine, and nothing says which part was the runner's. The agent knows the runner
processes (it lists the job, `procTree.pids`) and can read their memory (`procMemory`), but reports neither; the heartbeat
has only node-wide `memory`.

## Decision Drivers

* A fact, not an inference: "the runner memory is released" must be something a component knows and says, not a
  conclusion from RAM numbers (the repository's rule that state is reported, not guessed).
* Never claim "released" too early. A wrong "released" starts the large job into the very gap it is meant to avoid; a
  wrong "still held" only makes the caller wait.
* No dependence on Ollama internals that change between versions (runner program names, command lines, the blob a runner
  loaded).
* Fresh enough for a caller that waits on it: seconds, not the 30 s of a full OLLAMA report.
* Platforms that cannot tell say nothing instead of something false.

## Considered Options

* **A heartbeat block `ollama_runners: {count, private_mib}`: every process in the Ollama child's job except the server
  and the console host Windows starts for it, with their private working sets summed; absent where the agent cannot
  tell.**
* Infer "released" from the node's RAM (`memory.ram_available_gib` rising by the model's size). Rejected: it is the
  heuristic the bench already uses; other programs move RAM too, the model's RAM need is not known exactly, and file pages
  the runner read move to the standby list and show as available before or after the private memory is gone.
* Infer it from an empty `/api/ps`. Rejected: that is exactly the state in which the runner still holds the memory.
* Per model: attribute each runner to the model it serves, by reading its command line (`--model <blob>`) and matching the
  blob to `/api/ps`. Rejected for now: it needs reading another process's command line (process environment block), and
  the runner's arguments and program name have changed between Ollama versions; a match that silently fails would read
  as "no runner for this model", the dangerous direction. The node-wide count answers the bench's case (it unloads
  everything before the large job).
* Identify runners positively by program name (`ollama.exe runner`, `llama-server.exe`) or by Ollama's program folder.
  Rejected: names and layout are Ollama's to change, and a process the rule does not recognise would be left out, again
  the dangerous direction. The chosen rule leaves out only what is known not to be a runner.
* Put the block into the OLLAMA frame instead of the heartbeat. Rejected: that frame carries Ollama's API state and is
  sent on change; a memory figure changes all the time and would need a change threshold. The heartbeat is sampled every
  `heartbeat_interval_s` (2 s in the example configuration) and already carries per-process facts (`children_vram_mib`).
* Linux: list the Ollama child's process group from `/proc`. Not now: the agent's Linux supervisor does not list a
  child's tree yet (`procTree.pids` is Windows only); the block stays absent there until it does.

## Decision Outcome

Chosen option: "a heartbeat block `ollama_runners`", because it reports what the agent knows directly, errs towards
"still held", and does not depend on how Ollama names or starts its runners.

* Agent 0.37.0, `agent/ollamarunners.go`: `Supervisor.ollamaRunners` takes every running child whose program is Ollama
  (`isOllamaExe`; usually one, one per GPU is possible, and the node's fact is their sum), lists its job
  (`procTree.members`, which reports whether the job could be listed in full; if one cannot, the node gets no fact) and
  counts every process except the server (the child's own pid) and the system console host
  (`%SystemRoot%\System32\conhost.exe`, compared by full path): Windows starts one for a console program that has no
  console, as Ollama under the service, and puts it into the program's job (`TestConsoleHostIsNoRunner` shows it).
* `private_mib` sums the runners' private working sets (`procMemory`, ADR-0092): the RAM they hold that only paging out
  would free. File pages of the model are not counted; they go to the standby list when a runner exits.
* A process whose program cannot be read counts as a runner; one whose memory cannot be read counts with 0 MiB. So
  `count` reaches 0 only when the job holds nothing but the server and its console host.
* The block is absent when the agent cannot tell: Ollama is not its child (the tray app), not running (held for an
  update, restarting), its job could not be listed, or the platform is not Windows. An OpenAI-compatible upstream is no
  Ollama child, so it gets no block.
* Router 0.44.0 (`poll.note_ollama_runners`): `count` 0 to 256 and `private_mib` 0 to 4 TiB, both integers; anything
  else is dropped as implausible (ADR-0137), so a bad block reads as unknown, never as 0. The node keeps the block from
  the latest heartbeat; `Node.runners_known` gives it only while that heartbeat is fresh (`heartbeat_stale_s`). It shows
  in `/admin/state` (`nodes.<name>.ollama_runners`, `null` when unknown) and as metrics
  `skirnir_node_ollama_runners` and `skirnir_node_ollama_runner_private_gib`, absent for a node that cannot tell.
* Routing does not use the block; it is a fact for operators and tools, and for the unload of ADR-0149.

### Consequences

* Good, because "count 0" is the release itself: no runner process lives, so none holds memory. A tool no longer guesses
  from RAM.
* Good, because the rule does not know Ollama's runner names; a new runner program is counted without a change.
* Good, because an older agent, a Linux node or the tray app simply do not send the block, and the router and the view
  say "unknown".
* Bad, because the count is node-wide: with two models loaded and one unloaded, the count says one runner is left, not
  which. A caller that unloads one model of several compares the count with the models `/api/ps` still lists.
* Bad, because any other process Ollama starts in its job (a crash reporter, a helper of a future version) keeps the count
  above 0, and an unload waiting for 0 then times out instead of answering. That is the safe direction, and the answer
  carries the count and memory to look at.
* Bad, because a process that has ended may still be listed for the moment the system tears it down; the count drops a
  heartbeat later.

### Confirmation

`agent/ollamarunners_test.go` (all platforms): the server and the console host are no runners, two runners are summed, a
process whose memory cannot be read still counts, the wire form is `{"count":n,"private_mib":n}`, and without a running
Ollama child there is no block. `agent/ollamarunners_windows_test.go` (Windows CI): in a real job a grandchild counts as
one runner with memory and the count drops to 0 when it ends; a program with its own console has a console host in its
job, and it is not counted; through the supervisor two Ollama children are summed, another child is not counted, and a
tree that cannot be listed leaves the node without a fact. `agent/protocol_conformance_test.go` and `test/selftest_protocol.py`: the block is in
`docs/protocol/heartbeat.schema.json` with the router's reader. `test/selftest_ollama_runners.py` (CI): the router stores
the block, drops out-of-range or mistyped values as implausible (keeping the rest of the heartbeat), shows it only while
the heartbeat is fresh, and writes the two metrics only for nodes that report it.
