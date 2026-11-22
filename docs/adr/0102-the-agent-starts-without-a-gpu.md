---
status: proposed
date: 2026-11-23
decision-makers: operator
---

# The agent starts without a GPU, and the router sizes such a node by main memory

> Part of opening Skirnir to other setups (portability, phase 0). Builds on
> [ADR-0022](0022-components-report-state-as-facts.md) (components report state as facts) and
> [ADR-0023](0023-explicit-control-flow-and-error-codes.md) (no control flow from errors).

## Context and Problem Statement

Until agent 0.35 the agent refused to start without an NVIDIA GPU: `newGPU` failed when neither NVML nor `nvidia-smi`
was found, and `newApp` returned that error. A GPU sample that failed also aborted the whole heartbeat. A machine with an
AMD or Intel card, Apple silicon or no GPU at all (a CPU inference box, a small server that only runs an embedding model)
could therefore not join, even though Ollama runs there and the agent's other work (tunnel, children, Ollama updates,
memory facts) does not need a GPU.

The router had the matching gap: a node's model budget is free VRAM plus what Ollama holds, or the card size. A node
that reports no VRAM has a card size of 0, so nothing ever fits.

## Decision Drivers

* Existing GPU nodes and configurations behave exactly as before.
* The agent says that it has no GPU as a fact; the router never infers it from a missing field or an error (ADR-0022).
* The smallest design that routes sensibly to a CPU node: no new protocol frame, no second budget model, no new
  required setting.
* What makes no sense without a GPU (power limit, GPU-Z, GPU busy detection, the "model partly in system RAM" warning)
  is not applicable, and not reported as a problem.

## Considered Options

* **Agent: `gpu.probe: auto | nvidia | none`, default `auto`; fact `gpu_present`. Router: budget from the reported main
  memory.**
* Agent only: start without a GPU, let the router keep its VRAM budget. Rejected: such a node would register but never
  serve a model.
* Router: a configurable per-node memory budget (policy key) instead of the reported RAM. Rejected for now: it is a
  second number to keep in step with the machine, while the agent already reports available and total RAM every
  heartbeat (agent 0.17.0). It can be added later as an upper bound if an operator wants to reserve RAM for other work.
* A GPU abstraction for AMD/Intel/Apple in the same step. Rejected here: it needs measurement code per vendor; "no GPU"
  is the common base case and lets those machines join now (as CPU nodes) until a vendor probe exists.

## Decision Outcome

Chosen option: `gpu.probe` with fact `gpu_present`, budget from main memory.

Agent (0.36.0):

* `gpu.probe: auto` (default) probes NVIDIA as before (NVML, then `nvidia-smi`). Only where the agent used to refuse to
  start (neither found) it runs without a GPU. `nvidia` keeps the refusal for operators who want a missing driver to
  stop the service; `none` does not look. Whether `nvidia-smi` was found is a returned value (`findNvidiaSmi` returns
  path and a boolean), not an error that is interpreted.
* The facts at registration and every heartbeat carry `gpu_present` (true or false). Without a GPU the heartbeat leaves
  out the GPU values and sensors and still carries memory, game, guard, updates and the rest.
* The GPU guard reports the state `not_applicable` (reason `no_gpu`, `problem: false`) and does not run, also when
  `gpu_guard.enabled` is false. GPU-Z is not opened and `POST /gpuz` answers 409. The GPU sensor groups of the Home
  Assistant device are decided "does not apply" (removed if left over).
* `/health` and `status` show `gpu_probe` and `gpu_present`; the `gpu` diagnostic verb always probes strictly.
* A GPU sample that fails on a node *with* a GPU still aborts that heartbeat, as before; this ADR does not change it.

Router (0.36.0):

* `gpu_present: false` in the facts (registry) or a heartbeat marks the node as computing in main memory; a missing field
  means a GPU, as every older agent had one. A heartbeat updates the value.
* For such a node, what Ollama holds is each loaded model's whole `size` (on a CPU node `size_vram` is 0), and nothing is
  `ram_spill`, so no "partly in system RAM" problem.
* Budget = `memory.ram_available_gib` + what Ollama holds - reserve, where the reserve is at least
  `modes.memory_pressure.min_free_gib` (a load must not push the node into memory pressure, which would stop cold loads
  there). Capacity for "model too large" and "role possible when free" is `memory.ram_total_gib`. Prewarm uses the same
  budget. Without a fresh heartbeat the memory is unknown and nothing fits, instead of guessing.
* There is no GPU busy detection for such a node (no utilisation, no foreign VRAM); game facts still apply if the agent
  reports them.
* The guard state `not_applicable` is a known state: it does not cap the node, even with
  `modes.gpu_guard.require_fresh_status`.

### Consequences

* Good: a machine without an NVIDIA GPU joins with the same setup as any node and serves the models that fit its RAM.
* Good: nothing changes for GPU nodes: their heartbeat only gains `gpu_present: true`, budgets and states are the same.
* Bad: `auto` changes what a GPU node does when its driver is missing entirely (no `nvidia-smi` anywhere): it now starts
  as a CPU node instead of refusing. A driver that is installed but failing still leaves `nvidia-smi` in place, so the
  node stays a GPU node. Operators who want the old refusal set `gpu.probe: nvidia`.
* Bad: on Linux, `MemAvailable` can include page cache of a memory-mapped model, so "available + held" may overstate the
  room by part of the loaded model. Ollama checks memory itself before loading, and memory pressure stops further cold
  loads; the router's budget is a routing estimate, not a guarantee.
* Bad: the VRAM measurement of the model catalog (`/admin/measure`) needs a GPU; on a node without one it does not
  complete. The tokens-per-second benchmark works.
* Neutral: AMD, Intel and Apple GPUs run as CPU nodes until a vendor probe exists (a later ADR).

### Confirmation

`test/selftest_cpu_node.py` (router: facts and heartbeat without GPU values, full-size holdings, no ram_spill, budget
and capacity from RAM, model too large / waits, guard not capping, older agents unchanged), the end-to-end suite (a fake
agent with `FAKE_GPU=0` registers, comes online and raises no problem) and `agent/nogpu_test.go` (probe `none`, heartbeat
and facts without GPU values, guard not applicable, configuration).
