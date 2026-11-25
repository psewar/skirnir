"""Browser security headers on every response (TM-03, ADR-0065, ADR-0128, ADR-0130).

The control port serves the operator UI: ui.html with its script as the file /ui.js and its styles as the file /ui.css,
no inline script, no inline event handler (elements name actions in data-* attributes), no style element or attribute
(utility classes; the script sets computed sizes through CSSOM), nothing loaded from elsewhere. Its
Content-Security-Policy runs only scripts and applies only style sheets from the router itself: injected markup with an
inline handler or an inline script does not run, and injected inline CSS (overlays that disguise controls, attribute
selectors that leak values) is not applied. The policy also forbids what injected content
would need to do harm beyond the page: loading code or images from elsewhere, connecting anywhere but the router
(connect-src 'self' stops exfiltration by fetch or WebSocket), framing the UI (clickjacking), plugins, a changed base URL
and form posts elsewhere. Both ports send nosniff and no referrer. No CORS header is ever set, so other origins cannot
read answers. A header a handler set on purpose is kept.
"""
CONTROL_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
               "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
COMMON = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
CONTROL = {**COMMON, "Content-Security-Policy": CONTROL_CSP, "X-Frame-Options": "DENY"}


def _setter(headers):
    async def on_prepare(request, response):
        for name, value in headers.items():
            response.headers.setdefault(name, value)
    return on_prepare


def install(api, ctl):
    api.on_response_prepare.append(_setter(COMMON))
    ctl.on_response_prepare.append(_setter(CONTROL))
