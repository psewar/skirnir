---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Log unexpected errors without their message

> Source: TM-15 in the threat model. Complements ADR-0056 (owner-only router files and log canaries).

## Context and Problem Statement

A handler exception that no code expected (for example a database error from the session store) reached aiohttp, which
answered 500 and logged "Error handling request" with the full traceback. The last traceback line is the exception text,
and that text can carry data: a database error quoting a value, a backend reply, a fragment of a prompt. The canary test
for the session path (`test/selftest_log_hygiene_paths.py`) showed such a text in the log.

## Decision Drivers

* No user data or backend reply in the log, also on paths nobody anticipated.
* An operator must still be able to find where an unexpected error happened.
* The client must not learn internals from the answer.

## Considered Options

* A last-resort middleware on the API and control apps: log the exception type, method, path, a reference and the code
  locations, never the message; answer a fixed 500 with the same reference
* Redact known data patterns from exception messages before logging
* Leave it to aiohttp and rely on each handler catching its own errors

## Decision Outcome

Chosen option: "Log type and code locations, never the message", because only withholding the message is safe for data
the router cannot recognise. Pattern redaction misses what it does not know, and relying on every handler leaves exactly
the unanticipated paths open.

* `unexpected.unexpected_error_middleware` is the outermost middleware of both apps. Deliberate HTTP answers
  (`web.HTTPException`) pass unchanged; cancellation is not caught.
* The log line reads `unexpected <Type> in <METHOD> <path> (ref <id>); message withheld, code locations:` followed by file,
  line, function and source line of each frame. Chained exceptions and the message are not logged.
* The client receives `{"error": {"message": "internal error", "type": "internal_error", "ref": "<id>"}}`, status 500,
  `Cache-Control: no-store`.

### Consequences

* Good, because an unexpected exception can no longer copy data into the log or the answer.
* Good, because the reference ties the client's report to the log line.
* Bad, because the message is lost for debugging; reproducing the error is needed to see it.
* Neutral, because errors handled by their own code keep their existing, deliberate log lines.

### Confirmation

`test/selftest_log_hygiene_paths.py` `test_session_paths_keep_canaries_out_of_log_and_journal`: a session-store exception
carrying a canary is answered with `internal_error` and a reference, the log names the type and the reference, and the
canary is in neither the log nor the answer. Without the middleware the test reports the canary in the log.
