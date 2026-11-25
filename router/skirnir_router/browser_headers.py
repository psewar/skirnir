"""Browser security headers on every response (TM-03, ADR-0065, ADR-0128).

The control port serves the operator UI: ui.html with its script as the file /ui.js, no inline script and no inline event
handler (elements name actions in data-* attributes), nothing loaded from elsewhere. Its Content-Security-Policy runs only
scripts from the router itself: injected markup with an inline handler or an inline script does not run. Inline styles
stay allowed (the page uses style attributes; CSS cannot run script). The policy also forbids what injected content
would need to do harm beyond the page: loading code or images from elsewhere, connecting anywhere but the router
(connect-src 'self' stops exfiltration by fetch or WebSocket), framing the UI (clickjacking), plugins, a changed base URL
and form posts elsewhere. Both ports send nosniff and no referrer. No CORS header is ever set, so other origins cannot
read answers. A header a handler set on purpose is kept.
"""
CONTROL_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
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
