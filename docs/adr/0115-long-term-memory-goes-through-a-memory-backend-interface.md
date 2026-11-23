---
status: proposed
date: 2026-11-23
decision-makers: maintainer (proposed by Claude)
---

# Long-term memory goes through a memory backend interface

> Part of the portability roadmap, phase 3 ([design/portability.md](../../design/portability.md)). Extends
> [ADR-0028](0028-optional-hindsight-long-term-memory.md) (optional Hindsight long-term memory) and keeps
> [ADR-0030](0030-shared-memory-scopes-and-agent-interoperability.md) (shared scopes),
> [ADR-0060](0060-shared-memory-scopes-name-their-writers.md) (named writers) and
> [ADR-0076](0076-deletion-lifecycle-across-stores-and-restores.md) (deletion lifecycle). Adapter rule:
> [ADR-0118](0118-adapters-live-in-the-repository-and-are-chosen-by-configuration.md).

## Context and Problem Statement

Hindsight is the only memory backend, and its wire format reaches into the core:

* `router/skirnir_router/hindsight.py` holds `HindsightClient` (line 53) with `health`, `recall`, `retain`, `operation`,
  `delete_document`, `list_memories` and a raw `control` call; its callers are `memory.py`, `memory_outbox.py`,
  `memory_gateway.py` and `context_ops.py`.
* `MemoryService.recall` in `router/skirnir_router/memory.py` (from line 239; the parsing around lines 261-300) reads
  Hindsight's answer directly: the `results` list, each fact's `id` and `text`, and `scores.reranker` for the minimum
  score. The startup code builds the service with a `HindsightClient` (around line 486).

Using mem0, Zep, Letta or a plain pgvector store would mean editing `memory.py`, the outbox and the gateway. The
safeguards around memory (scope and grant checks, named writers, provenance, recall budgets, deletion journal and
reconciliation, TM-09, TM-10, TM-12) are written against the current client and must hold for any backend.

## Decision Drivers

* Other memory systems without edits to the scope, grant, budget and deletion logic.
* The safeguards stay in the router and apply to every backend in the same way.
* Hindsight installations keep working without configuration changes.
* Report backend state as facts (`bank_missing`, `unavailable`), not as backend-specific errors.

## Considered Options

* A `MemoryBackend` interface; a Hindsight adapter that normalises answers to a `Fact` type.
* Keep Hindsight only; other systems can put a Hindsight-compatible API in front of themselves.
* Pass backend answers through untouched and let clients parse them.

## Decision Outcome

Chosen option: "A `MemoryBackend` interface with a normalising Hindsight adapter", because the router keeps all policy
and only the transport and the answer format vary per backend.

* Interface: `health`, `recall -> list[Fact] | Problem`, `retain`, `operation`, `delete_document`, `list`. `Fact` carries
  id, text, type, score(s) and provenance as the router uses them today.
* The Hindsight adapter is today's client plus the normalisation now done inline in `MemoryService.recall`; the service
  works only with `Fact`.
* Further adapters (mem0, pgvector) when someone asks for them, each with the same self-tests as Hindsight.

### Consequences

* Good, because a new memory system is one adapter and its tests.
* Good, because scope, grant and deletion checks are written once, against `Fact`.
* Bad, because a common `Fact` loses features only one backend has (Hindsight observations and their sources need a
  mapping, not a drop).
* Bad, because deletion reconciliation after a restore (ADR-0076) needs each backend to delete by document id; a backend
  without that cannot be offered with the outbox.

### Confirmation

`test/selftest_memory.py` and the context and interoperability tests run against the Hindsight adapter unchanged; a new
self-test runs the recall budget, minimum score and scope checks against a fake backend that returns `Fact` values. The
live memory integration (`test/integration_live_memory.py`, `.github/workflows/live-memory.yml`) keeps testing the real
Hindsight.
