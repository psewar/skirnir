---
status: proposed
date: 2026-11-12
decision-makers: operator
---

# Windows children keep their data outside the agent's folder

> Source: the ADR-0088 rollout on a Windows node (agent 0.34.0), 2026-11-12, and a probe of Ollama's file access under
> LOCAL SERVICE. Amends ADR-0088 (where a child's own data folder lives); supersedes nothing.

## Context and Problem Statement

ADR-0088 gives each LOCAL SERVICE child modify on its own folder **inside** the agent's data folder
(`C:\ProgramData\skirnir-agent\<folder>`), and never grants the data folder itself, which holds the configuration and
keys. On the first node Ollama, with its models moved there, started healthy but reported every manifest as
`bad manifest ... Access is denied`. The grants were in place down to the files.

A probe started the same checks once as LocalSystem and once as a process with a LOCAL SERVICE token:

| Operation on the model folder | LocalSystem | LOCAL SERVICE |
|---|---|---|
| open, read, open for writing a manifest; list folders; `os.Root`, `os.DirFS`; create a temp file | ok | ok |
| `filepath.EvalSymlinks` on the model folder and on a manifest | ok | **Access is denied** |

Ollama 0.40 is a Go program that contains `filepath.EvalSymlinks` next to its manifest path checks (`manifest.pathWithin`,
`resolveManifestPath`); the probe reproduced the error with exactly that call and no other. On Windows Go
normalises every component of a path with `FindFirstFile`, which needs the right to **list the folder above** each
component. LOCAL SERVICE may not list the agent's data folder, and must not: that is where the configuration and keys
are. A traverse entry (`X`) on the data folder was tried and did not help. Bypass traverse checking lets a child open a
path below a folder it cannot list, but not resolve it.

So a child's data inside the agent's folder works for programs that open paths directly (the STT service in Python),
and fails for programs that resolve them (Ollama, any Go program, others that canonicalise paths).

## Decision Drivers

* The agent's folder stays closed to the children, including its file names.
* A child's data folder works whichever way the child's program opens paths.
* The agent derives the location itself and checks it before every start (the tool does its job including the checks);
  a path a child cannot resolve is a fact in the status, not an error to read from a child's log.
* No new configuration key, nothing to set up by hand.

## Considered Options

1. **A children's data folder next to the agent's** (`<data folder>-children`, by default
   `C:\ProgramData\skirnir-agent-children`), created and protected by the agent; each child's own folder is the first
   level below it.
2. **Let LOCAL SERVICE list the agent's data folder** (`RD`, this folder only).
3. **Keep Ollama on `run_as: system`.**
4. **A folder named per child in the configuration**, anywhere, granted like `write_paths`.

## Decision Outcome

Proposed: **option 1.**

* Option 2 is rejected: every child, and every Windows service running as LOCAL SERVICE, would see the names of the
  configuration, key and identity files, and the agent's folder would no longer be closed to them.
* Option 3 is rejected as the answer: it is the stopgap in place until this lands (with `run_as_reason`), and it gives
  Ollama back the system rights ADR-0088 took away.
* Option 4 needs a path per child in every configuration and the ADR-0085 rules on each one; option 1 derives the place
  and keeps the rule "modify only on the child's own folder".

How it works:

* **Place.** The children's data folder is the agent's data folder with `-children` appended, in the same parent folder
  (`ProgramData`). A path in it becomes modify on the child's own folder there (the first level), created when missing,
  like before in the agent's folder. The children's data folder itself and files directly in it are never granted.
* **Protection.** When missing, the agent creates the folder (only when it is about to start a child, never from
  `check-config`) with its own access list, inherited from nothing: system and administrators full control, LOCAL
  SERVICE may list this folder only, so every child can resolve the paths below it. Each child's own folder gets its
  modify entry from the grant of ADR-0088. Before granting anything below it, the agent checks that the folder is no link
  and that nobody besides the system and the administrators can change it or a folder above (the ADR-0085 rules: users
  may create folders in `ProgramData`, so someone could create it first). Otherwise each child with data there is
  refused with that fact; the remedy is to remove the folder, the agent creates it anew.
* **Resolve check.** After access is established, the agent resolves each path as the child, with the real
  `filepath.EvalSymlinks`. The result is a new fact per entry, `resolve`: `ok`, or the first folder above the path that
  the child cannot list. It is not a refusal: programs that open paths directly work without it (a read path in a
  user's profile, for example), and the agent cannot tell which kind a child is. It is logged at every start and printed
  by `check-config`.
* **Data still in the agent's folder** keeps its grant, so a node keeps working across the update. The entry carries
  `move_to`, the folder it belongs in, logged at every start and printed by `check-config`; moving it is an operator step
  (stop the child, move the folder, change the path in the configuration).

### Consequences

* Good, because Ollama (and any Go child) can run as LOCAL SERVICE with its models in a folder the agent manages.
* Good, because the agent's folder stays closed, including its file names.
* Good, because a path a child cannot resolve shows up as a fact before anyone reads a child's log.
* Bad, because there are now two folders under `ProgramData` for one agent.
* Bad, because LOCAL SERVICE, shared with other Windows services, can list the children's data folder (the children's
  folder names, nothing in them unless granted).
* Bad, because data in the agent's folder is only reported, not moved; each node needs the move once. A later version
  can refuse it there, once no node has any.

### Confirmation

* Tests:
  * derivation: a path in the children's data folder is modify on the child's own folder there, with no `move_to`; the
    folder itself and a file directly in it are never granted; data in the agent's folder carries `move_to`;
  * the folder is created only when starting, with a protected access list (LOCAL SERVICE may list this folder only, no
    inheritance), and a folder others can change refuses the children with data in it, without a grant;
  * the incident itself (Windows CI, elevated): a folder the child may read below a folder it may not list opens, does
    not resolve, and the resolve fact names the folder above; once that folder is listable it is no longer named.
* Node check during the rollout:
  * `check-config` shows the `move to` lines for the data still in the agent's folder;
  * after the move, Ollama on `run_as: localservice` lists its models and loads one, and `resolve` is `ok` for its
    folders;
  * the STT service runs from its moved folders.

## More Information

ADR-0088 (LOCAL SERVICE children and their grants), ADR-0085 (only folders nobody else can change), ADR-0087. Threat
model TM-14.
