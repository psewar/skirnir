---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Shared memory scopes name their writers

> Source: TM-10 in the threat model. Complements ADR-0059 (no automatic recall into requests with tools).

## Context and Problem Statement

A shared memory scope (`router.memory_scopes`) is one bank read by every client whose project grant names it. Who could
write into it was decided only on the client side: any grant with `scope: <name>` and `write: true` made that client a
writer. Adding a client with a copied grant silently widened the set of writers of a bank that other clients, possibly
with tools, read automatically. Shared-memory poisoning starts with an unexpected writer.

## Decision Drivers

* The set of writers of a shared bank must be visible in one place, next to the scope.
* Widening it must take a deliberate change on the scope, not only on a client.
* A configuration that contradicts itself is refused at load time with a message naming client and scope.

## Considered Options

* `memory_scopes.<scope>.writers`: a list of client names; a write grant on the scope loads only for a listed client;
  absent or empty means nobody may write
* Keep writer rights on the client grant only
* A separate role (`curator`) that alone may write shared scopes

## Decision Outcome

Chosen option: "The scope names its writers", because it makes write access two-sided without a new role and keeps the
list where a reviewer of the shared bank looks. A curator role would mix writing with the existing deletion grant
(`read` + `delete` on a shared scope).

* `writers` is a list of at most 64 distinct configured client names. An unknown name, a write grant of an unlisted
  client, or a malformed list stops loading with a message naming the scope and client.
* Absent or empty `writers` means nobody may write the scope. Configurations from before this change that grant writes on
  a shared scope must add the list (breaking for such configurations).
* The router checks the list again per request (`scope_for_principal`), so a live policy can never widen what loading
  refused. Reading and deletion grants are unchanged.

### Consequences

* Good, because every writer of a shared bank is listed on the bank's scope.
* Good, because a copied client grant no longer adds a writer by accident.
* Bad, because existing configurations with shared-scope writers must be updated before the new router loads them.
* Neutral, because a listed writer is still trusted to write correct facts; the list limits who, not what.

### Confirmation

`test/selftest_memory.py`: `test_shared_scope_names_its_writers` (missing, empty, wrong, unknown, duplicate or malformed
lists refused; a listed writer and a read-only client load) and
`test_writing_a_shared_scope_needs_the_scopes_writer_list_at_runtime` (an unlisted client's retain is refused with 403,
reading needs no entry, an emptied list stops the listed writer).
