---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# Windows agent: children without system rights, the agent's own rights split later

> Source: TM-14 follow-up (risk register: "separate privileged Windows agent operations"). Proposal: the options and a
> measurement plan, before any code. Builds on ADR-0085.

## Context and Problem Statement

On Windows the agent runs as LocalSystem. It did run as the virtual account `NT SERVICE\SkirnirAgent` until the GPU guard
(ADR-0019) arrived, and was moved to LocalSystem on purpose:

* Setting the power limit through NVML needs administrator rights. The virtual account got NVML rc 4, and the guard
  reported itself unavailable (measured 2026-09-24).
* Reading GPU-Z's shared memory from session 0 (`NtOpenSection` on the session's `BaseNamedObjects`) is denied to the
  virtual account.

Other functions also rely on those rights today, without the dependency being written down:

* The game detection reads the paths of every process (`OpenProcess` with `PROCESS_QUERY_LIMITED_INFORMATION`) and every
  loaded user hive under `HKEY_USERS`, to find the game folders.
* The Ollama registry scan reads the same user hives.
* The self-update and the Ollama update replace files in `C:\Program Files`.

The agent starts its children with its own rights: Ollama (llama.cpp), Strata (a Python server and a native engine) and the
speech-to-text service (Python, CUDA). They are by far the largest code on the node, and they handle input from the network:
prompts, tool calls and audio. A memory-safety bug in any of them yields SYSTEM today. The agent's own code is small Go,
talks only to the router (signed updates, an authenticated tunnel) and does not parse model input.

ADR-0085 closed the path where someone else could swap a child's program. The child itself still runs as SYSTEM.

## Decision Drivers

* Take SYSTEM away from the code that handles untrusted input first.
* Keep the GPU guard. It protects the hardware: the 12V-2x6 connector of the GPU.
* Keep game detection and the GPU hold during games working.
* Measure before building: every right a child or the agent loses must be shown to be unnecessary on a real node.
* Nothing breaks silently. A function that loses its right reports that as a fact, as the guard already does with
  `unavailable`.

## Considered Options

1. **Children under a separate low-privilege identity, the agent stays LocalSystem.** Each child gets a token without
   administrator rights. Candidate tokens: a restricted copy of the agent's token (administrators and the user SID
   deny-only, privileges removed, integrity medium), the built-in LOCAL SERVICE account (nothing to create), or later an S4U
   logon of a local account created by setup (no password stored). The child gets read access to its
   program and model folders and write access only to a data folder of its own.
2. **Split the agent.** An unprivileged main agent (`NT SERVICE\SkirnirAgent`: tunnel, children, MQTT) plus a small
   LocalSystem helper. The helper accepts a fixed set of operations through a named pipe that only the main agent's
   service SID may open:
   * set the power limit, clamped to [minimum, default];
   * read GPU-Z;
   * the process and game-folder facts;
   * apply an update after the helper has checked the signature itself.
3. **The whole agent under the virtual account, with grants.** This loses the GPU guard and GPU-Z (measured) and probably
   the game paths. Rejected as a single step.
4. **Keep everything as it is.** ADR-0085 already removed the swap path; only the children's own bugs remain.

## Decision Outcome

Chosen (accepted by the operator 2026-11-12): **option 1 now, option 2 as a later step.**

Option 1 moves the untrusted-input code out of SYSTEM and leaves the agent's privileged functions untouched. Option 2
only shrinks what remains, the agent's own small code, and costs a second service and an IPC interface.

Implementation outline for option 1, to be confirmed by the measurements below:

* `children[].run_as: restricted | system` (default `restricted` after the measurements; `system` only with a stated
  reason, like `user_writable_reason`). The supervisor creates the process with the token through `CreateProcessAsUser`
  and keeps the job objects.
* `setup` and `install` create the identity and grant it access, derived from the child's configuration rather than
  listed by hand:
  * read access to the program folder (ADR-0085 folders) and to the model folders named in `env` or `args`;
  * write access only to a data folder of its own under `C:\ProgramData\skirnir-agent\<child>`.
* `check-config` and the status report each child's identity as a fact.

### Measurement plan (on a node, administrator; nothing changes in the running service)

The probe verb `skirnir-agent probe-identity` (agent 0.33.0) runs as LocalSystem; started by an administrator it runs
itself once as LocalSystem through a scheduled task, and only from a folder no one else can change. For every enabled
child (the upstream child only with `--with-upstream "<reason>"`, it loads a large model) and each token (`system` as the
baseline, `restricted`, `localservice`) it starts the child's command line on a probe port of its own, so the running
child is not disturbed, and reports:

1. Whether it starts and its health port answers; otherwise its exit code and the last lines of its output (access
   denied, missing write access to a folder).
2. Whether its output shows CUDA (Ollama's "inference compute ... library=cuda"; the STT service's CUDA load).
3. Strata's "runtime directory must be owner-only" requirement is met with the child's own data folder.
4. The per-process GPU memory counter still attributes the child's VRAM to it (`gpuproc_windows.go`), so the router does
   not count it as foreign.
5. Job objects still end the whole process tree on restart.

Under each token it also runs a copy of itself that checks the agent's own privileged functions. LOCAL SERVICE stands in
for the virtual account here, since both are service identities without administrator rights. This answers what option 2
would need:

* NVML set-to-current-value (a no-op that shows the permission);
* GPU-Z `NtOpenSection`;
* paths of other users' processes;
* readable user hives.

### Consequences

* Good, because a bug in llama.cpp, Strata or the STT stack no longer yields SYSTEM; the router-facing agent code stays
  as tested.
* Good, because the GPU guard and the game detection keep their rights.
* Bad, because the agent itself remains LocalSystem until option 2.
* Bad, because new folder grants appear. A child whose models lie in a user's profile (the Ollama models, Strata's 103 GB)
  needs read access there. That is data, not code (ADR-0085), and the grant is read-only.
* Bad, because `CreateProcessAsUser` and S4U add Windows-specific code paths that only a real node can test.

### Confirmation

When accepted:

* the probe results recorded in this ADR's follow-up;
* Windows tests for token creation and a refused `run_as: system` without a reason;
* a node check in which Ollama, the STT service and Strata (on request) answer under the new identity, and the agent's
  guard still sets the power limit.

## More Information

Threat model TM-14. ADR-0019 (GPU guard needs LocalSystem), ADR-0085 (children only from protected folders).
