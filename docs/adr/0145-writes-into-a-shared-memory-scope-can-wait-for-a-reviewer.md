---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude while working through the open risk-register items)
---

# Writes into a shared memory scope can wait for a reviewer before they reach the bank

> Threat: TM-10 (next action "consider fact review before shared facts are recalled"; validation gap V9).
> Builds on [ADR-0030](0030-shared-memory-scopes-and-agent-interoperability.md) (shared scopes),
> [ADR-0059](0059-no-automatic-recall-into-requests-with-tools.md) (no automatic recall into requests with tools),
> [ADR-0060](0060-shared-memory-scopes-name-their-writers.md) (shared scopes name their writers) and
> [ADR-0114](0114-the-session-store-is-chosen-by-its-connection-url.md) (SQLite store for single hosts).

## Context and Problem Statement

A shared memory scope is one bank that several clients read. Facts get into it like this:

1. A writer retains text: through `POST /v1/skirnir/memory/retain`, the Hindsight gateway, or automatic output retention.
2. The router admits the text into the durable outbox and delivers it to the backend.
3. Hindsight extracts facts from it and consolidates observations across all sources of the bank.
4. Every reader recalls them: by automatic recall, the REST recall, the gateway, or `recall_with` next to a private bank.

Two decisions already limit this. ADR-0060 limits *who* may write: the scope lists its writers. ADR-0059 keeps
automatic recall out of requests with tools, unless a grant opts in. What a listed writer writes is still trusted as it
is (V9). Such a writer can be a compromised agent, or an agent that copies an injected instruction from a web page into
memory. Its false fact or planted instruction reaches every reader:

* as data in requests without tools;
* in clients that opted into `recall_into_tools`;
* through explicit recall to agents that then act on it.

Curators can list and delete facts (ADR-0030), but only after the facts may already have been recalled. By then
consolidated observations may have absorbed them.

After extraction the router can no longer tell which recalled fact came from which source: observations lose their
source metadata (ADR-0059). The only point where content is still one writer's unit, with its writer and provenance, is
the outbox before delivery.

## Decision Drivers

* Text from a writer that is not trusted must reach no reader of the shared bank before someone reviewed it. This must
  hold on every read path and be enforced in the router.
* The check must not depend on metadata the backend keeps or loses ([ADR-0115](0115-long-term-memory-goes-through-a-memory-backend-interface.md)).
* Off by default. Existing scopes behave as before.
* Who reviews is named on the scope, next to `writers`, as in ADR-0060.
* Waiting is a visible state, never a silent delay that looks like missing knowledge.
* Unreviewed text does not pile up without bound.
* A model verdict is not a permission (AGENTS.md).

## Considered Options

* **Chosen: hold writes before delivery until a reviewer approves them, per scope.**
* **Mark reviewed memory units and filter at recall.** Rejected:
  * Consolidated observations mix reviewed and unreviewed sources.
  * The mark would live in the backend.
  * The filter would have to cover four read paths.
* **Recall only facts written by the reader or by trusted writers (provenance filter).** Rejected for the same reason
  as in ADR-0059: observations lose the metadata, and a client's own poisoned writes would still pass.
* **A staging scope plus promotion by a curator.** Agents write into a staging scope that only a curator reads. The
  curator writes approved facts into the shared scope, whose only writer it is. This works today without code. Not
  chosen:
  * every fact is written twice, by hand or by a curator tool;
  * the original writer's provenance is replaced by the curator's;
  * Hindsight extracts everything twice.

  It remains possible as an interim pattern.
* **Screen writes automatically with a classifier or model.** Rejected as a gate:
  * it is a heuristic with false negatives;
  * an attacker adapts to it;
  * a model verdict is not a permission.

  It may later help a reviewer by highlighting suspicious text.
* **Review in the control UI by the admin.** Not part of this decision. The control port shows only aggregate memory
  state today, never content (`design/shared-memory-interoperability.md`). Showing payloads there widens what admin
  sessions and the browser see (B2, TM-03). That needs its own ADR.
* **Keep the current controls and accept V9.** Possible: the maintainer accepts the residual risk with an expiry in the
  risk register, and TM-10 drops its "consider" item.

## Decision Outcome

Chosen option: "hold writes before delivery until a reviewer approves them", because:

* the outbox is the only point where the router sees one writer's text unmixed;
* holding it there covers every read path at once;
* it needs nothing from the backend.

### Configuration

```yaml
router:
  memory_scopes:
    shared-research:
      data_class: personal
      allow_cloud: false
      writers: [agent-a, agent-b]
      review: true                 # default false
      reviewers: [curator-a]       # required when review is true
      review_expire_days: 14       # default 14, 1 to 365
      review_max_held: 100         # per writer, default 100, 1 to 10000
```

* `reviewers` follows the rules of `writers`: at most 64 distinct configured client names. Each one needs `read` on a
  project bound to the scope.
* Loading refuses each of these:
  * `review: true` with no reviewers;
  * an unknown reviewer, or one without `read`;
  * `review: true` without the durable outbox (`hindsight.outbox_enabled`), because a held write must survive a restart.

### What is held

* A write into the scope by a writer that is not a reviewer is admitted durably with the state `awaiting_review`,
  instead of `pending`. The outbox worker never delivers it.
* This applies to every write path: REST retain, gateway retain and automatic output retention. All of them go through
  the outbox's `submit`.
* A reviewer's own writes go straight to `pending`, because a reviewer could approve them anyway. Review limits the other
  writers, not the reviewers.
* A replay of the same event returns the existing operation, as today. So a rejected write cannot come back under the
  same event id; a new event id is held again.
* Enabling review does not touch facts already in the bank. Curators list and delete those as before.

### Review interface

The interface is REST under `/v1/skirnir/memory`, for reviewers of the scope only, like curation:

* `POST /review` with `{project, limit?, offset?}` returns one page of held operations:
  * the operation id;
  * the writer and the admission time;
  * the provenance;
  * the text as submitted, which is exactly what the backend would extract from.
* `POST /operations/{uuid}/approve` with `{}` moves the operation to `pending`, so delivery starts.
* `POST /operations/{uuid}/reject` with `{}` moves it to the new terminal state `rejected` and scrubs the payload at once.
* Approve or reject by a client that is not a reviewer of the scope answers 404, so it does not reveal the operation.
  This includes the operation's own writer, which can still read its status.
* Approval checks the grants again at that moment. A writer removed from `writers` meanwhile turns the operation into
  `revoked`, as today.
* Each decision is logged with reviewer, writer and scope, without content. It is counted as
  `skirnir_memory_events_total{operation="review",outcome="approved|rejected|expired"}`.
* A small command-line tool (`tools/memory_review.py`: list, show, approve, reject) uses a reviewer's credential, so a
  person can review without writing HTTP calls. It reads the credential from the environment, never from its arguments.

### Waiting is visible

* The writer's operation status says `awaiting_review`. The gateway reports it as still processing.
* `/admin/context` shows the number of held operations per scope, as a count only. So does the gauge
  `skirnir_memory_outbox_jobs{status="awaiting_review"}`.
* After `review_expire_days` a held operation becomes `rejected` with the reason `review_expired`, and its payload is
  scrubbed.
* Held operations count against `outbox_capacity` and `outbox_client_capacity`, as today. `outbox_client_capacity` is
  off by default, so on its own one writer could fill the whole outbox with held writes for up to `review_expire_days`.
  That would block every other client's memory writes. Therefore each writer may hold at most `review_max_held`
  operations per scope. Past that, only this writer's further writes into the scope are refused (503, problem
  `review_backlog_full`); other clients are not affected.

### Storage

`awaiting_review` and `rejected` are new outbox states. They need a new migration for PostgreSQL, where the state column
has a CHECK constraint, and for SQLite (ADR-0114). The migration only adds; existing rows are unchanged.

### Consequences

* Good, because nothing that a non-reviewer writes reaches any reader of the shared bank before a reviewer approved it.
  This holds on every read path and does not depend on the backend.
* Good, because the reviewer sees the exact text, writer and provenance before extraction.
* Good, because review is off by default, and the scope names who reviews.
* Bad, because shared knowledge appears only after review. Without an active reviewer, writes wait until they expire.
  A writer that needs a fact at once can also keep it in its private bank.
* Bad, because the reviewer reviews the source text, not the extracted facts. The backend's extraction can still turn
  correct text into a wrong observation. That is a model error, not a writer attack.
* Bad, because a reviewer is a trusted writer, and approval is judgement, not proof of truth.
* Bad, because the new states need a migration. A router without it refuses the new schema version, so a rollback
  follows the design document's rollback rules.
* Neutral, because review holds only writes that go through the router. Direct access to the backend bypasses it, as it
  bypasses every grant (B5, TM-11).
* Neutral, because ADR-0059 and ADR-0060 stay as they are. A client that opts into `recall_into_tools` still depends on
  its own tool confirmation, but now only for reviewed facts.

### Confirmation

The implementing pull request brings:

* A self-test `test/selftest_memory_review.py`, without router and network, with a stub backend that records every
  delivery:
  * The configuration is refused for each case listed above.
  * A writer's retain becomes `awaiting_review` and is never delivered.
  * A reviewer's retain is delivered directly.
  * Approval delivers. Rejection and expiry never deliver, and they scrub the payload.
  * Gateway retain and automatic retention are held the same way.
  * Rejected cases: a writer cannot approve its own operation, a reviewer of another scope gets 404, and so does a
    client without a grant.
  * A writer past `review_max_held` is refused, while another writer of the same scope is still admitted.
  * A replayed event of a rejected write stays rejected.
  * A writer removed from `writers` before approval gets `revoked`.
* The store integrations on SQLite and PostgreSQL (ADR-0114): the migration, and that a held operation survives a
  restart.
* TM-10, V9, the threat model and the design document in English and German.
