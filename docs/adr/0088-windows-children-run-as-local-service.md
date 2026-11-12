---
status: accepted
date: 2026-11-12
decision-makers: operator
---

# Windows children run as LOCAL SERVICE, with folder access the agent derives and grants

> Source: ADR-0087 (option 1 accepted, measurement plan) and its measurements with `probe-identity` (agent 0.33.1) on a
> Windows node, 2026-11-12. Supersedes nothing; implements option 1 of ADR-0087.

## Context and Problem Statement

ADR-0087 chose to take SYSTEM away from the agent's children first: Ollama (llama.cpp), the speech-to-text service and
Strata handle network input and are by far the largest code on a node. It left open which identity they get and how
they get access to their folders, to be decided by measurement.

The measurements (`skirnir-agent probe-identity`, agent 0.33.1, same node, each child on a probe port of its own):

| Child / function | `system` (today) | `restricted` | `localservice`, no grants | `localservice`, with folder grants |
|---|---|---|---|---|
| STT service | healthy, CUDA, 2076 MiB | exits at once, 0xC06D007E (a module cannot be loaded) | log folder not writable | healthy after 2 s, CUDA, 2427 MiB attributed |
| Ollama | healthy, CUDA | model folder in a user profile not readable | same | healthy after 2 s, CUDA (no model loaded) |
| Process tree ends with the job object | yes | yes | yes | yes |
| NVML (the agent's own GPU guard) | set allowed | init fails (rc 9) | set denied (rc 4) | set denied (rc 4) |
| Paths of other processes / user hives | 665 of 669 / 1 of 1 | 23 of 668 / 0 of 1 | 21 of 666 / 0 of 1 | 19 of 666 / 0 of 1 |

The grants of the last run were temporary (removed afterwards, checked): read on the Ollama and Whisper model folders,
modify on three folders below `C:\ProgramData\skirnir-agent` that the children write (Ollama's home, the STT log and
collection folder, an OpenVINO cache), traverse on the folders above. The agent's own functions need its current rights
(option 2 of ADR-0087, later).

## Decision Drivers

* Take SYSTEM away from the code that handles untrusted input (ADR-0087).
* Grant only what a child needs, derived from its configuration rather than listed by hand twice.
* The tool does its job including the checks: the agent checks each folder as the child's account before it starts the
  child and grants what is missing; nobody has to remember a grant step after changing a child.
* Nothing breaks silently, and nothing falls back to more rights silently.

## Considered Options

1. **LOCAL SERVICE, access derived, checked and granted by the agent before every start.**
2. **The restricted token** (a copy of the agent's token with administrators deny-only, no privileges, medium integrity).
3. **LOCAL SERVICE, access granted once by `setup` or `install`.**
4. **A local account of its own, logged on through S4U** (no stored password).
5. **One virtual service account per child** (`NT SERVICE\<child>`), which needs a Windows service per child.

## Decision Outcome

Chosen (accepted by the operator 2026-11-12): **option 1.**

* Option 2 is out by measurement: NVML cannot even initialise and the STT service cannot load its modules.
* Option 3 would leave a child broken after a configuration change (a new model folder) until someone runs setup again.
  The agent has the configuration at every start anyway.
* Options 4 and 5 would separate the children from each other and from other Windows services, but they need an account
  or a service per child. They stay possible later behind the same `run_as` key.

How it works:

* **Identity.** `children[].run_as` is `localservice` (the default on Windows) or `system`. `system` is accepted only with
  `run_as_reason: "<why>"`, which stays in the status and the log, like `user_writable_reason` (ADR-0085). The agent logs
  on `NT AUTHORITY\LOCAL SERVICE` (a built-in account, no password, nothing to create) and starts the child with that
  token (`CreateProcessAsUser`) and with the account's own environment, plus the child's `env`. Job objects and the
  per-process GPU memory attribution are unchanged. On Linux the children run as the agent's own account (ADR-0084), and
  the keys must stay empty.
* **What the child needs.** The agent derives it from the child's configuration. Candidates:
  * the program's folder and `cwd`;
  * every `env` value and every argument (or `--flag=value`) that is exactly one absolute path, so lists like `PATH` are
    skipped;
  * `read_paths` and `write_paths` for folders the command line does not name, like models listed in a file the child
    reads.
* **Read or modify.**
  * A path below the agent's data folder (the folder of `config.yaml`) becomes **modify on the child's own folder** there,
    the first level below the data folder. It is created when missing.
  * The data folder itself and files directly in it (configuration, keys) are never granted.
  * Every other path is **read** (read and execute; inherited on folders).
  * A drive root, a system folder (Windows, Program Files, ProgramData, the profiles folder) or a whole user profile is
    only checked, never granted; a narrower folder has to be named.
* **Check, then grant.** Before every start, each path is checked as LOCAL SERVICE: the agent impersonates the account
  and lists the folder or opens the file, or for modify creates and removes a file. Only a failed check leads to a grant
  (`icacls … /grant *S-1-5-19:(OI)(CI)(RX)` or `(M)`), and the check runs again.
  * Folders above are not granted. LOCAL SERVICE holds "bypass traverse checking", so the traverse grants of the
    measurement are not repeated; the node check confirms it.
  * **Never through a link.** A path that is, or lies below, a junction, symbolic link or cloud folder is not granted.
    Otherwise whoever may replace a configured folder with a link (a model folder in a user's profile) would choose where
    the agent, as LocalSystem, grants access. `icacls` runs with `/L`, so a link swapped in after the check receives the
    entry itself, not its target.
  * **Modify** outside the child's own data folder (`write_paths`) is granted only where nobody besides the system and
    the administrators can change the path or a folder above (the ADR-0085 rules); a user could otherwise swap a folder
    above for a link between the check and the grant and give LOCAL SERVICE write access to a program folder.
* **Facts, not errors.** The status reports `children[].run_as`, `run_as_reason` and `access` (path, need, where it was
  derived from, state: `ok`, `granted`, `missing`, `not granted: <why>`, `denied: <why>`). Grants are logged.
  `check-config` prints the same, checks without granting, and from an administrator's console says that only LocalSystem
  can check.
* **Refusal.** A child whose access cannot be established (the grant fails, or the check still fails after it) is
  refused like an exposed program (ADR-0085). It is checked again every minute and never started as SYSTEM instead.

### Consequences

* Good, because a bug in llama.cpp, the STT stack or Strata no longer yields SYSTEM; LOCAL SERVICE has no administrator
  rights, cannot set the GPU power limit and cannot read other users' hives or most process paths (measured).
* Good, because grants follow the configuration by themselves and are visible as facts.
* Bad, because LOCAL SERVICE is shared: all children and the Windows services running as LOCAL SERVICE get the same
  grants. They can read the granted model folders (data, not code) and modify each child's own data folder. Options 4 and
  5 would separate them.
* Bad, because grants stay after a child is removed or moved to `system`. The documentation gives the removal
  (`icacls <folder> /remove:g *S-1-5-19`).
* Bad, because a read grant on a model folder inside a user's profile is a lasting change to that profile's folder.
* Bad, because a read grant on a folder its user can change keeps a small race: swapping a folder above it for a link in
  the moment between the check and the grant moves the read entry elsewhere. It gives LOCAL SERVICE read, not write; the
  modify case is closed by the rule above.
* Open: Strata (not measured; its runtime folder must be owner-only) and Ollama loading a model under LOCAL SERVICE (the
  probe loads none). Both are part of the node check. If Strata cannot run, it gets `run_as: system` with its reason until
  fixed.

### Confirmation

* Tests:
  * derivation (`childAccessPlan`: env, arguments, cwd, the agent's own files, deduplication, parents first);
  * the too-broad rule;
  * check and grant (`ensureAccess`: check only for `check-config`, created own folder, failed and ineffective grants);
  * no grant through a junction or below one, no modify on a folder its user can change;
  * the real entry `icacls` leaves, inherited by a file already in the folder;
  * impersonation (a folder only administrators may read is denied to another token and readable after a grant, and the
    calling thread keeps its own identity);
  * configuration validation (`run_as: system` without a reason, unknown values, keys on Linux);
  * the supervisor refusing a child whose access cannot be established, with the facts in its status.
* Node check during the rollout:
  * Ollama loads a model and answers;
  * the STT service shows CUDA;
  * Strata answers on request;
  * the GPU guard still sets the power limit;
  * the status shows `run_as: localservice` and the access of each child.

## More Information

ADR-0087 (options, measurement plan), ADR-0085 (children only from protected folders), ADR-0084 (Linux agent unit),
ADR-0019 (GPU guard needs LocalSystem). Threat model TM-14.
