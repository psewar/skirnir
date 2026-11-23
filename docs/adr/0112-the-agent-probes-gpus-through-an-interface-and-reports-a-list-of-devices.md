---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# The agent probes GPUs through an interface and reports a list of devices

> Part of the portability roadmap, phase 2 ([design/portability.md](../../design/portability.md)). Extends
> [ADR-0102](0102-the-agent-starts-without-a-gpu.md) (the agent starts without a GPU). Builds on
> [ADR-0025](0025-measured-vram-instead-of-estimates.md) (measured VRAM) and
> [ADR-0019](0019-gpu-guard-power-limit-on-by-default.md) (GPU guard). Depends on the versioned protocol specification
> ([ADR-0107](0107-versioned-router-agent-protocol-with-a-written-specification.md)). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

Since ADR-0102 the agent either measures exactly one NVIDIA GPU or runs without one:

* `gpu.probe` (`auto`, `nvidia`, `none`; `agent/config.go`, line 96) selects in `newGPU` (`agent/gpu.go`, from
  line 60): NVML first, else `nvidia-smi`, else (with `auto`) no GPU. The heartbeat and the facts carry `gpu_present`,
  and a node without a GPU computes in main memory on the router side.
* NVML is bound only on Windows (`agent/gpu_nvml_windows.go`); on Linux `openNVML` always fails
  (`agent/gpu_nvml_other.go`, lines 7-11) and every sample starts an `nvidia-smi` process.
* NVML opens device 0 only (`nvmlDeviceGetHandleByIndex_v2` with index 0, `agent/gpu_nvml_windows.go`, around line 101).
* With a GPU present, the heartbeat aborts when a sample fails and labels the error `nvidia-smi` (`agent/heartbeat.go`,
  lines 63-67), and sends one set of VRAM and utilisation fields (`vram_total_mib`, `vram_used_mib`, `vram_free_mib`,
  `gpu_util_pct`) that the router reads in `apply_heartbeat` (`router/skirnir_router/poll.py`, around line 728).
* The GPU guard sets the power limit through a hook `setLimit` (`agent/guard.go`, line 142), already an abstraction of
  sorts, implemented by NVML or `nvidia-smi`; without a GPU it reports `not_applicable`.

AMD (ROCm), Intel Arc and Apple Silicon machines can therefore join only as nodes without a GPU (main-memory budget,
no GPU measurement), multi-GPU machines report only their first card, and Linux nodes are measured more coarsely than
Windows nodes.

## Decision Drivers

* Nodes with other vendors or several GPUs must report correct memory, because the scheduler places models by measured
  VRAM (ADR-0025).
* No cgo and no new build dependency: the agent is built with CGO off and reproducibly (ADR-0075).
* The GPU guard keeps working where a power limit can be set, and says so as a fact where it cannot.
* Older routers and agents keep talking to each other during the transition.

## Considered Options

* A `GPUProbe` interface with one implementation per vendor and a `gpus[]` list in the heartbeat.
* Keep NVIDIA only; other vendors stay "no GPU" nodes (the state since ADR-0102).
* Call vendor command-line tools (`nvidia-smi`, `rocm-smi`, `powermetrics`) everywhere.

## Decision Outcome

Chosen option: "A `GPUProbe` interface and a list of devices", because it opens other vendors and multi-GPU without
touching the scheduler's model of measured memory.

* Interface: `Devices()`, `Sample()`, `PowerLimits()`, `SetLimit()`. A probe that has no lever reports that as a fact;
  the guard then reports a state that says so (like `not_applicable` without a GPU) instead of failing.
* Implementations: `none` (ADR-0102), NVML on Windows (today) and on Linux (loaded with `dlopen`-style dynamic
  loading of `libnvidia-ml.so` without cgo, like the Windows DLL binding), `nvidia-smi` as fallback; later ROCm SMI,
  Intel and Apple (`powermetrics` / IOKit).
* Heartbeat: a `gpus[]` list with per-device memory, utilisation and identity. The top-level fields stay, filled from
  device 0 or the sum, until every supported router reads the list. The router computes free VRAM per device.

### Consequences

* Good, because AMD, Intel and Apple machines and multi-GPU nodes can join with real measurements.
* Good, because Linux NVIDIA nodes get the same NVML precision as Windows nodes and stop forking a process per sample.
* Bad, because per-device placement makes the scheduler's fit check more complex (a model may fit the sum but no single
  card unless the runtime splits it).
* Bad, because loading a vendor library at run time is a new native boundary on the node; it must be loaded only from
  the system's library path and fail closed to `none`.
* Bad, because each vendor needs hardware to test on; untested probes stay off by default.

### Confirmation

Go tests with fake probes for the heartbeat (`gpus[]` and the compatible top-level fields), for the guard with and without
a lever, and for a node with two devices. A router self-test feeds a two-GPU heartbeat and checks the per-device free
memory used by the fit check. `docs/protocol/heartbeat.schema.json` documents `gpus[]` (ADR-0107), and the
protocol conformance tests (`agent/protocol_conformance_test.go`, `test/selftest_protocol.py`) cover it. `go vet` for Windows and Linux and `go test ./...` in CI; CGO stays off in the
CI builds (`.github/workflows/ci.yml`) and the reproducible release rebuild (`.github/workflows/release-provenance.yml`).
