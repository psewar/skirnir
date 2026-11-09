---
status: accepted
date: 2026-11-10
decision-makers: operator
---

# Deletion lifecycle across stores and restores

> Source: TM-12 review on 2026-11-09 (deletion paths, crash and restore behaviour of sessions, memory outbox and
> Hindsight).

## Context and Problem Statement

Content lives in three places: sessions and the memory outbox in PostgreSQL, extracted documents in Hindsight. Each had
its own deletion, but nothing tied them together across failures:

* Deletion tombstones (outbox rows in state `deleted`) lived in the same database as the data. Restoring an older
  database brought deleted sessions back and, worse, pending outbox rows with their content, which the worker then sent
  to Hindsight again.
* Restoring Hindsight brought deleted documents back; nothing re-applied the router's deletions.
* Deleting a session left the memory retained from its answers, and that operation's id was never shown to the owner,
  so it could not be deleted either.
* A deletion requested while a send was in flight was refused (409); a deletion that ended dead or revoked was invisible.
* The opt-in decision capture grew without bound.

## Decision Drivers

* A deletion the router confirmed stays effective after restoring either store.
* No content in new places; ids only.
* Existing behaviour stays the default where it is a policy choice (memory may outlive its session on purpose).

## Considered Options

* A deletion journal outside the database, replayed at start, plus a Hindsight reconcile; optional deletion of a
  session's memory
* Tombstones in a second database
* Documentation only (restore procedures)

## Decision Outcome

Chosen option: "journal, replay, reconcile, opt-in session memory deletion", because a small append-only file next to the
configuration is restored independently of the database and needs no second service; the replay runs before the outbox
worker starts, so nothing restored can leave first.

* `deletions.jsonl` next to `config.yaml` (owner-only): one line per completed API deletion, `{ts, kind, owner, id}`.
  Retention deletions are not journaled; the same retention rule removes restored expired sessions again.
* When the session store opens, `replay_deletions` deletes journaled sessions again and marks journaled operations (and,
  with the option, the operations of journaled sessions) like an API deletion: never sent -> `deleted` without payload,
  possibly in Hindsight -> `delete_pending`, in flight -> `delete_requested`. A journal that cannot be read, or a database
  error, stops the start. The log names how many came back.
* `POST /admin/memory/reconcile-deletions` (control port, admin): pages through operations in state `deleted` that were
  sent, and deletes their Hindsight document again; a missing document counts as deleted; counts only in the answer and
  the audit log.
* Memory operations record their session (`memory_outbox.session_id`, schema migration). `session_store.
  delete_memory_with_session` (default false) marks them in the same transaction as the session deletion, also for
  retention deletions. The stored scope decides as for any deletion: without the `delete` grant the job ends revoked.
* `outbox_finish` turns an ended send of a row with `delete_requested` into `delete_pending`.
* Responses of inference that may be retained carry `X-Skirnir-Memory-Operation`.
* Gauge `skirnir_memory_deletions_stuck`; posture infos for sessions without retention, memory kept on session deletion
  and outbox payloads without retention; the decision capture keeps 64 MiB plus one rotated file.

### Consequences

* Good, because restoring the database or Hindsight no longer undoes deletions, and restored pending sends of deleted
  knowledge are never sent.
* Good, because an owner can delete what an answer added, and can choose that a session takes its memory with it.
* Bad, because the journal must not be restored older than the database; that is an operational rule.
* Bad, because the journal grows by one line per deletion and is read at every start.
* Neutral, because derived Hindsight observations are Hindsight's to remove with their document; the live test checks
  the document, not every derived fact.

### Confirmation

`test/selftest_deletion_lifecycle.py` (journal format, torn and malformed lines, replay hand-over, unreadable journal
stops the start, operation header, reconcile paging and validation, capture rotation, posture infos).
`test/integration_deletion_lifecycle.py` on PostgreSQL: session deletion with and without the option, other owners and
sessions untouched, deletion requested in flight taken over at finish, retention with memory, a simulated restore
replayed (session gone, pending send marked, second replay a no-op), stuck count and reconcile listing.
