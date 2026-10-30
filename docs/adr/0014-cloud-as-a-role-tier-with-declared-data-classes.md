---
status: accepted
date: 2026-09-10
decision-makers: operator
---

# Cloud is a tier in roles; data classes are a client's declaration

> Recorded retroactively on 2026-10-30; decided on 2026-09-09, changed the same day from opt-in per client to roles plus
> client opt-out on 2026-09-10. Source: `design/roadmap.md` (decisions table, "Wo die Spezifikation reibt" 4 and 5),
> README "Cloud as a tier in roles".

## Context and Problem Statement

Some requests cannot be served locally (no node, a missing capability). Cloud models could answer, but prompts from the
home may contain personal data, and the house rule is "local before cloud".

## Decision Drivers

* By default nothing leaves the house.
* Where cloud is allowed is a decision about the role, not every client.
* No fail-closed content classifier that blocks every cloud path when unsure.

## Considered Options

* Cloud models as tiers in roles (usually last); data classes declared by the client with a server-side floor (default
  `personal` blocks cloud); credential scan, monthly budget, egress allowlist; clients can opt out
* Cloud as opt-in per client
* Automatic classification of request content as the gate

## Decision Outcome

Chosen option: "Cloud tiers in roles with declared data classes", because the role says where cloud may be a tier and a
cold local model still comes before the cloud. A client's data class is a declaration with a floor, not content analysis:
an uncertain classifier as the only gate would block every cloud path. Prompts with recognisable keys never go out.

### Consequences

* Good, because roles stay local by default and cloud use is explicit and budgeted.
* Bad, because a client that declares nothing (`personal`) never reaches cloud; ADR-0038 later added an opt-in content
  check for such clients.

### Confirmation

Selftests for data-class floors, credential scan and budget; the decision journal records skipped cloud tiers with the
reason.
