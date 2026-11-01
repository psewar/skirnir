---
status: accepted
date: 2026-09-10
decision-makers: operator
---

# The client describes, the router decides

> Backfilled on 2026-10-30, decided on 2026-09-09/10 (roadmap stage 1). Source: `design/roadmap.md`,
> README "The client describes, the router decides".

## Context and Problem Statement

Clients need different things from a model: tools, images, structured output, thinking, a minimum context, a deadline.
With roles (ADR-0003) a client no longer picks the model, but it still has to be able to say what the answer needs.

## Decision Drivers

* Clients state requirements, not machines or models.
* A request that no tier can satisfy should fail clearly, not wait.
* Clients that send nothing extra keep working unchanged.

## Considered Options

* An optional `routing` block (required/preferred capabilities, minimum context, priority, session, data class,
  request id), plus requirements derived from the body (tools, images, format, think); routing information in headers
  and, on request, in the body
* Encode requirements in model names (one role per combination)
* Let clients pick models themselves

## Decision Outcome

Chosen option: "An optional `routing` block plus derived requirements", because the router can match it against
capabilities from Ollama (`/api/show`) and the catalog and keep roles few. Unsatisfiable requirements answer 400 instead
of 503; the chosen node, model and tier come back as `X-Skirnir-*` headers.

### Consequences

* Good, because a client can say "needs vision and 32k" without knowing which model has it.
* Good, because answers are traceable to node and tier.
* Bad, because capabilities from Ollama are not always right; catalog overrides correct them (for example `structured:
  false` for a model whose JSON mode fails).

### Confirmation

Selftests for derived capabilities and the 400 path; E2E tests check the routing headers.
