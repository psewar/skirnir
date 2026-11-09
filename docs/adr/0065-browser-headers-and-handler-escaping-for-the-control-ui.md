---
status: accepted
date: 2026-11-09
decision-makers: operator
---

# Browser headers and handler escaping for the control UI

> Source: TM-03 in the threat model.

## Context and Problem Statement

The control UI (ui.html) is one page with an inline script, inline event handlers and inline styles. Cross-site actions
were already refused (custom header, Fetch Metadata), but the port sent no browser security headers, and the UI put
values into inline handlers as `onclick="f('${esc(x)}')"`. `esc()` did not escape the single quote, and even with it the
browser decodes `&#39;` back to `'` before the handler's JavaScript runs. A model name reported by a node, which a
compromised approved agent controls, could therefore break out of the string and run script in the operator's browser.

## Decision Drivers

* A value from the router state must never become code in the operator's browser.
* Injected script that slips through must not be able to send data elsewhere, frame the UI or load code.
* No rebuild of the UI; the protection must be checkable in CI.

## Considered Options

* Content-Security-Policy and companion headers on both ports, plus `js()` for every value inside an inline handler
  (a JSON string literal, then HTML-escaped) and `esc()` escaping the single quote; a static test guards both
* Move all handlers to `addEventListener` and drop `'unsafe-inline'`
* Headers only

## Decision Outcome

Chosen option: "Headers plus `js()` in handlers", because it closes the injection where it happens and limits the damage of
anything missed, without rewriting a 120 KB page. A strict CSP without `'unsafe-inline'` remains a later option; headers
alone would leave the injection in place.

* Control port: `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'
  'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none';
  form-action 'self'`, `X-Frame-Options: DENY`. Both ports: `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer`. No CORS header is set anywhere. A header a handler sets on purpose is kept.
* ui.html: `esc()` escapes `'`; `js(s) = esc(JSON.stringify(String(s)))`; every interpolation in an inline handler is
  `${js(...)}`, except a loop index and a fixed property name.
* `test/dev_env.py` opens the control port on loopback with the development switch (ADR-0053), so the UI can be checked
  in a browser again.

### Consequences

* Good, because a crafted model, role, client or node value is passed as data; checked in a browser with a model name
  that tried to set a global variable.
* Good, because injected script cannot reach any origin but the router (`connect-src 'self'`, checked in a browser).
* Bad, because `'unsafe-inline'` stays: injected markup with an inline handler would still run inside the page.
* Neutral, because the API port serves no page and gets no CSP.

### Confirmation

`test/selftest_browser.py`: headers on UI, admin JSON, refusals and preflights, none of them with a CORS header; API port
headers; `esc()` and `js()` definitions; every inline-handler interpolation goes through `js()`; the UI loads nothing from
another origin. Fails against the previous state (4 failures).
