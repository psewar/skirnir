---
status: proposed
date: 2026-11-25
decision-makers: maintainer (proposed by Claude)
---

# The control UI uses no inline style

> Supersedes [ADR-0128](0128-the-control-ui-runs-no-inline-script.md) (the control UI runs no inline script), which named
> dropping `'unsafe-inline'` for styles as the later option. Its script decision stays as it was: `/ui.js`, named actions
> in `data-*` attributes, `script-src 'self'`. What changes is the style policy and where the page's styles live.
> Threat: TM-03.

## Context and Problem Statement

After ADR-0128 the control port's Content-Security-Policy still allowed `style-src 'unsafe-inline'`: `ui.html` carried
its styles in one `<style>` element, and the page and the markup its script builds used 52 `style` attributes (4 of
them with computed values: legend colours and the width of the cloud budget bar). CSS cannot run script, but injected
markup that is allowed inline styles can still do harm with the operator's session: lay a transparent or disguised
element over a control so that a click lands somewhere else, or hide or fake a warning (a pending node, a failed
check). (Reading values out through CSS selectors and requests is already stopped by `img-src`/`default-src 'self'`.)
The risk register kept this open (TM-03, next action "consider dropping 'unsafe-inline' for styles").

## Decision Drivers

* Injected markup must not change how the page looks or where clicks land, whatever value slipped through an escape.
* The page looks and behaves exactly as before; no build step, no framework, no dependency.
* Installation, deployment and the container image keep shipping the same program files.

## Considered Options

* **The styles as a file (`/ui.css`), utility classes in place of style attributes, computed values through CSSOM;
  `style-src 'self'`.**
* Keep the `<style>` element and allow it by its hash. Rejected: style attributes cannot be allowed by a hash without
  `'unsafe-hashes'`, so they have to go either way; with them gone a file is simpler than a hash recomputed for every edit.
* A nonce per response. Rejected: the page is served as a file; a nonce needs templating and still not attributes.
* Keep `'unsafe-inline'` for styles (ADR-0128's state). Rejected now that the script work showed the conversion is
  mechanical and checkable.

## Decision Outcome

Chosen option: "the styles as a file, utility classes and CSSOM", because it removes inline style entirely with a
policy the browser enforces, and leaves the look of the page unchanged.

* `router/ui.css` holds the former `<style>` element unchanged; `ui.html` loads it with
  `<link rel="stylesheet" href="ui.css">` where the element was. The control port serves it at `/ui.css` behind the same
  login as the page, `Cache-Control: no-store` like the page and its script. `install.sh` (which refuses a source folder
  without it), `deploy.py` and the router image ship it with `ui.html` and `ui.js`.
* Each static style attribute becomes utility classes (`w-48`, `fs-12`, `fw-normal`, `ml-auto`, ...) defined at the end
  of `ui.css`, merged into the element's `class` attribute. They are `!important`, so they keep the precedence a style
  attribute had over the element rules (an element rule such as `select { width:100% }` must not win over `w-150`).
* Computed values: the legend colours use classes `pal0`…`pal7` (`palClass(i)`), which carry the colours of the charts'
  palette `PAL`; the budget bar's width goes into `data-pct` and the script sets it through CSSOM
  (`element.style.width`), which the policy allows because it is not markup.
* Control port policy: `script-src 'self'; style-src 'self'` (no `'unsafe-inline'` anywhere, no `'unsafe-eval'`); all
  other directives and headers as in ADR-0128 and ADR-0065.

### Consequences

* Good, because injected markup cannot style anything: an injected `style` attribute and an injected `<style>` element
  were blocked as `style-src-attr` and `style-src-elem` violations in a browser, and a full-screen overlay injected
  that way did not appear.
* Good, because the control port's policy now contains no `'unsafe-inline'` at all.
* Bad, because there is one more program file to ship; an installation that gets `ui.html` without `ui.css` shows the
  page unstyled. `install.sh` refuses a source folder without it, and the image and deployment lists name it.
* Bad, because new markup must use a class instead of a quick `style` attribute; the self-test fails on one, and a class
  the markup uses but `ui.css` lacks.
* Neutral: script code may still set styles through CSSOM (`element.style...`, as before for showing and hiding
  elements); the policy does not restrict that, and it is not reachable by injected markup.

### Confirmation

`test/selftest_browser.py`: the policy's `style-src` is exactly `'self'` and the policy contains no `'unsafe-inline'`;
`/ui.css` is served with the page's headers, uncached, and refused without login; `ui.html` and the markup `ui.js`
builds contain no style element and no style attribute (the same patterns find the element and all 52 attributes in the
previous files), the script sets no style attribute; every utility class the markup uses is defined in `ui.css`; the
legend classes carry exactly the colours of `PAL`; `ui.css` loads nothing from elsewhere. In a browser against
`test/dev_env.py` (2026-11-25): for all 91 elements with a utility or legend class, the computed style and position
with the class equal those with the former style set inline (every computed property compared); no policy violation and
no console message while using all tabs; the budget bar takes the computed width.
