---
status: superseded by ADR-0130
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The control UI runs no inline script

> Supersedes [ADR-0065](0065-browser-headers-and-handler-escaping-for-the-control-ui.md) (browser headers and handler
> escaping), which named a strict policy without `'unsafe-inline'` as the later option. Its headers stay as they were;
> what changes is the script policy and how the page passes values to its handlers. Threat: TM-03.

## Context and Problem Statement

The control UI (`router/ui.html`) was one page with one inline script and 68 inline event handlers (`onclick`,
`onchange`, `oninput`), 48 of them built by the script with values interpolated as `onclick="f(${js(x)})"`. ADR-0065
closed the injection through those values (`js()`) and added a Content-Security-Policy, but the policy had to allow
`script-src 'unsafe-inline'`. Anything that still got markup into the page (a missed escape, a future template) could
carry an inline handler, and the browser would run it with the operator's session: approve or revoke nodes, change the
configuration, rotate client secrets. The risk register kept this open (TM-03, next action "move inline handlers to
addEventListener and drop 'unsafe-inline'").

## Decision Drivers

* Injected markup must not run as script, whatever value slipped through an escape.
* Values that reach a handler are data, never code (no JavaScript string assembled from them).
* The page keeps working exactly as before; no build step, no framework, no dependency.
* Installation, deployment and the container image keep shipping the same program files.

## Considered Options

* **The script as a file (`/ui.js`) and named actions in `data-*` attributes; `script-src 'self'`.**
* Keep the script inline and allow it by its hash in the policy. Rejected as the only change: inline handlers cannot be
  allowed by a hash without `'unsafe-hashes'`, which would let every handler with the same text run again; the handlers
  have to go either way, and with them gone a separate file is simpler than recomputing a hash for every edit.
* A nonce per response. Rejected: the page is served as a file; a nonce needs templating and still not handlers.
* Also drop `'unsafe-inline'` for styles. Not now: the page and the markup it builds use 47 style attributes; CSS cannot
  run script, so the gain is small against a rewrite of the layout. It stays a later option.

## Decision Outcome

Chosen option: "the script as a file and named actions", because it removes inline script entirely and passes values as
data, with a policy the browser enforces.

* `router/ui.js` holds the page's script; `ui.html` loads it with `<script src="ui.js">` where the inline script was.
  The control port serves it at `/ui.js` behind the same login as the page, `Cache-Control: no-store` like the page.
  `install.sh`, `deploy.py` and the router image ship it with `ui.html`.
* An element names its action in `data-click`, `data-change` or `data-input` and passes its arguments in `data-args`
  (a JSON array built by `args()`, which HTML-escapes it) or one string in `data-arg`. One listener per event type, in
  the capture phase on the document, walks the event's path from the target upwards (as inline handlers fired) and calls
  `ACTIONS[name](element, ...arguments)`. `ACTIONS` holds what each former handler did; conversions of `this.value` and
  `this.checked` are named in `VALUE`. Running in the capture phase keeps the former order: an action runs before the
  listeners of `#roles` see the event.
* Control port policy: `script-src 'self'` (no `'unsafe-inline'`, no `'unsafe-eval'`); `style-src 'self'
  'unsafe-inline'`; all other directives and headers as in ADR-0065.

### Consequences

* Good, because injected markup with an inline handler or an inline script does not run (checked in a browser: an
  injected `onerror`, `onclick` and `<script>` were blocked as `script-src-attr` and `script-src-elem` violations).
* Good, because a crafted model, role, client or node name reaches its handler as a JSON value; it is never part of a
  string that the browser compiles.
* Good, because one listener per event type replaces 68 handlers, and a self-test checks that every action an element
  names exists.
* Bad, because there is one more program file to ship; an installation that gets `ui.html` without `ui.js` shows an
  empty page. `install.sh` refuses a source folder without it, and the image and deployment lists name it.
* Bad, because `'unsafe-inline'` stays for styles: injected markup can still change how the page looks.
* Neutral: settings handlers now pass the raw setting path; before they passed the HTML-escaped one, which differed only
  for paths with `&<>"'` (none exist) and would have broken the list toggle there.

### Confirmation

`test/selftest_browser.py`: the policy's `script-src` is exactly `'self'`; `/ui.js` is served with the page's headers,
uncached, and refused without login; `ui.html` and the markup `ui.js` builds contain no inline handler and no inline
script (the same patterns find all 68 handlers and the script in the previous `ui.html`); every `data-args` value comes
from `args()`; every named action exists in `ACTIONS` and every action is named somewhere; nothing is evaluated as code.
In a browser against `test/dev_env.py` (2026-11-25): every action triggered with the values and conversions of the
former handler (66 checks), no policy violation while using all tabs.
