---
status: accepted
date: 2026-11-01
decision-makers: operator
---

# Writes to the admin API require the header `X-Skirnir-UI: 1`

> Source: `docs/security-review-2026-11-01.md` (SK-SEC-04), PR #70.

## Context and Problem Statement

The admin port uses HTTP Basic Auth. Browsers send Basic credentials automatically, also for requests that another site
triggers (a form posting to `/admin/config`). State-changing admin endpoints had no protection against such cross-site
requests.

## Decision Drivers

* Protection without sessions or cookies (the admin API has none).
* Scripts and the web UI must keep working with a small change.

## Considered Options

* A non-simple custom header on every non-GET request to `/admin/*` (`X-Skirnir-UI: 1`), plus refusing requests marked
  `Sec-Fetch-Site: cross-site`
* CSRF tokens issued by the UI
* Checking `Origin` / `Referer` only

## Decision Outcome

Chosen option: "Custom header plus Fetch Metadata", because a cross-site form cannot set a custom header, and a
cross-site script that tries needs a CORS preflight the router never grants. It needs no state on the server. Reading
(`GET`) stays as it was.

### Consequences

* Good, because a logged-in browser can no longer be tricked into changing the router's configuration.
* Bad, because every script that writes to `/admin/*` must send the header; scripts without it get 403 with a message
  naming the header.

### Confirmation

`test/selftest_security.py` and `test/run_tests.py` check that writes without the header, or marked cross-site, get 403
and that reads do not need it; the web UI adds the header to every request.
