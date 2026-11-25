---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The per-client body limit also bounds chunked bodies

> Supersedes [ADR-0063](0063-bound-concurrency-and-body-size-per-client.md) (bound concurrency and body size per
> client). Everything ADR-0063 decided stays; what changes is the body limit for requests without a declared length.
> Threat: TM-08.

## Context and Problem Statement

ADR-0063 gave every configured client `max_concurrent` requests in progress (default 4) and `max_body_mib` (1-64,
default 16), checked against the declared `Content-Length` before any work. A request with `Transfer-Encoding: chunked`
declares no length, so ADR-0063 left it bounded only by the router-wide ceiling of 64 MiB (`client_max_size`). A client
limited to 16 MiB could therefore send four times as much per request, and up to `max_concurrent` such bodies at once.
The risk register kept "bound chunked bodies per client" as an open next action.

## Decision Drivers

* The client's limit holds whatever the client declares or leaves out.
* Memory stays bounded while the body arrives, not only after it was read.
* A client cannot hold more bodies in memory than it has slots.
* Same answer and audit as for a declared size; handlers and request formats unchanged.

## Considered Options

* **Read a chunked body in the client middleware, inside the client's slot, with the client's limit as the request's
  `client_max_size`; 413 and audit `body_too_large` when it grows past it.**
* Pass the limit to every handler. Rejected: more than a dozen handlers read bodies (`read_json`, `request.read()`); one
  missed call would reopen the gap, and `read_json` reports any read failure as invalid JSON (400), not as 413.
* Refuse chunked requests on the API port. Rejected: clients may send chunked bodies (HTTP/1.1 allows it), and the
  limit can be kept without breaking them.
* Lower the router-wide ceiling. Rejected: it would apply to every client, including those allowed up to 64 MiB.

## Decision Outcome

Chosen option: "read chunked bodies in the middleware, within the limit and the slot", because it closes the gap at the
one place every API request passes, with aiohttp's own size check.

* Unchanged from ADR-0063: `max_concurrent` (1-256, default 4, 429 with `Retry-After: 1`), the declared-size check
  before any work (413), slots given back on completion, failure and disconnect, the router's own UI identity
  unlimited, per-client counters and peak gauges.
* New: when a configured client's request has a body without a declared length, the middleware takes the client's slot
  first, then reads the body through a copy of the request whose `client_max_size` is the client's `max_body_mib`
  (`common.read_body_bounded`). aiohttp checks the size while it reads. Past the limit the request gets 413 with the
  same text as for a declared size, the counter `too_large` and the audit line `body_too_large` (with `chunked: true`).
  Within the limit the handler gets that copy with the body already read and the fact `BODY_PREREAD` on it. A
  connection that fails while the body arrives is passed on; the handler's own read reports it as before.
* A handler that streams its body itself checks that fact instead of the used-up stream: `session_api.body` (session
  and memory APIs, own bound of 1 MiB) takes the bytes from the request and applies its 1 MiB bound to them.

### Consequences

* Good, because a client's limit now holds for chunked bodies: 17 MiB chunked for a 16 MiB client is refused while it
  arrives, and memory per client stays at most `max_concurrent` bodies of `max_body_mib`.
* Good, because handlers, formats and all limits for declared sizes stay as they were.
* Bad, because a chunked body is now read before the handler starts. The one handler that streams its body
  (`session_api.body`) uses the bytes the middleware read; a future streaming handler has to do the same (the self-test
  covers `session_api.body`).
* Bad, because a chunked session or memory write is read up to the client's limit (default 16 MiB) before the session
  API's own 1 MiB bound applies; with a declared length, or for the UI identity, it is still refused at 1 MiB while
  streaming.
* Neutral: the unauthenticated path and the router's UI identity keep the 64 MiB ceiling, as before.

### Confirmation

`test/selftest_client_limits.py`: 17 MiB chunked to a client with the default 16 MiB is answered 413, the handler does
not run, the counter and the audit line (`chunked: true`) are written and the slot is free again; 17 MiB chunked for a
client allowing 32 MiB and a chunked JSON body reach the handler intact (also through `read_json`); `session_api.body`
gets a chunked body the middleware read and still refuses one above 1 MiB; while a slow chunked upload is read it holds
the client's only slot, so a second request gets 429. The declared-size tests of ADR-0063 stay.
