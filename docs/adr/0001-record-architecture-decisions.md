---
status: accepted
date: 2026-10-30
decision-makers: operator
---

# Record architecture decisions as ADRs

## Context and Problem Statement

Skirnir grew in a few weeks from a single script into a router with an agent, a scheduler, cloud tiers, memory services
and OpenAI-compatible nodes. The design documents in `design/` and the changelog say *how* things work and *what*
changed, but the *why* is scattered over design notes, release notes, PR descriptions and conversations. Someone who
asks "why is it built like this and not otherwise?" has to dig.

## Decision Drivers

* The reasons behind the architecture must stay traceable after the people and conversations are gone.
* Changing a decision must not erase the old reasoning.
* Little overhead: one short file per decision, written with the change.

## Considered Options

* Architecture Decision Records in the repository (MADR 4.0)
* Keep reasons in the design documents
* Keep reasons in PR descriptions and release notes only

## Decision Outcome

Chosen option: "Architecture Decision Records in the repository (MADR 4.0)", because they keep one decision per file
next to the code, version it with the code, and make superseded reasoning visible instead of overwriting it.

Rules:

* Location and name: `docs/adr/NNNN-title-in-kebab-case.md`, numbered from `0001`; `docs/adr/README.md` lists number,
  title, status and date.
* One decision per ADR: context, drivers, options, outcome with reasons, consequences, how compliance is confirmed, who
  decided and when.
* Status: `proposed` → `accepted` | `rejected`; later `deprecated` or `superseded by ADR-NNNN`.
* Accepted ADRs are not rewritten. A changed decision gets a new ADR that supersedes the old one; the old one only
  changes its status, and both link to each other. Typos and links may be fixed.
* A PR that makes or changes a decision brings its ADR (or a proposed ADR before it) and names it in its description.
* Earlier decisions are recorded retroactively and marked as such ("recorded retroactively on …, decided on …") with
  their source.
* Design documents describe *how*; they link to the ADRs instead of repeating the reasons.

### Consequences

* Good, because the reasons for the current architecture are in one place, in order.
* Good, because superseded decisions stay readable with their reasons.
* Bad, because every decision costs a short extra document in the PR.

### Confirmation

Review of each PR: a PR that introduces or changes a technology, protocol, interface, security boundary, data store or
mode of operation names an ADR. `docs/adr/README.md` lists every ADR.

## More Information

Format: [MADR 4.0](https://adr.github.io/madr/), after Michael Nygard's original idea. ADR-0002 to ADR-0039 were recorded
retroactively on 2026-10-30 from the changelog, the release notes, `design/` and the PR history.
