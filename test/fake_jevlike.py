#!/usr/bin/env python3
"""Fake-Jevlike fuer die Tests: spricht das Protokoll aus decision/jevlike.py, entscheidet nach Markern im Kontext.

    python fake_jevlike.py <port>
Kontext enthaelt "[[code]]" (Optionsname in doppelten Klammern) -> diese Option 0,85, Rest verteilt.
Kontext enthaelt "UNSICHER" -> Platz 1 0,46, Platz 2 0,43 (Abschnitt 8: soll als unsicher gelten).
Kontext enthaelt "AUSFALL"  -> HTTP 500 (Engine faellt aus -> naechste in der Kette).
Sonst: erste Option 0,7.
"""
import sys
import time

from aiohttp import web

CALLS = []


async def decide(request):
    b = await request.json()
    ctx, options = b.get("context", ""), list(b.get("options") or [])
    CALLS.append(ctx[:80])
    if "AUSFALL" in ctx:
        return web.json_response({"error": "kaputt"}, status=500)
    n = len(options)
    marked = next((o for o in options if f"[[{o}]]" in ctx), None)
    if "UNSICHER" in ctx and n >= 2:
        rest = (1 - 0.46 - 0.43) / max(1, n - 2)
        probs = {o: rest for o in options}
        probs[options[0]], probs[options[1]] = 0.46, 0.43
    elif marked:
        probs = {o: 0.15 / max(1, n - 1) for o in options}
        probs[marked] = 0.85
    else:
        probs = {o: 0.3 / max(1, n - 1) for o in options}
        probs[options[0]] = 0.7
    return web.json_response({"probabilities": probs, "model": "fake-router-v1", "encoder": "tiny", "latency_ms": 2.0})


async def health(request):
    return web.json_response({"ok": True, "model": "fake-router-v1", "encoder": "tiny", "offline": True, "calls": len(CALLS)})


async def calls(request):
    return web.json_response({"calls": CALLS, "n": len(CALLS)})


app = web.Application()
app.router.add_post("/decide", decide)
app.router.add_get("/health", health)
app.router.add_get("/_calls", calls)
if __name__ == "__main__":
    print(f"fake-jevlike auf {sys.argv[1]} {time.strftime('%H:%M:%S')}", flush=True)
    web.run_app(app, host="127.0.0.1", port=int(sys.argv[1]), print=None)
