#!/usr/bin/env python3
"""Selbsttest (Router 0.8.9): Anbieterfehler werden am strukturierten Fehlercode unterschieden, nie am Meldungstext.

Bis 0.8.9 entschied proxy._via_cloud per str(e).lower().startswith("the model"), ob ein 4xx des Anbieters an den Client
geht oder ein anderer Kandidat helfen kann. Jetzt traegt CloudError den Code aus dem Fehlerkoerper (OpenAI error.code,
Anthropic error.type) und model_unavailable; proxy.cloud_client_fault entscheidet daran. Laeuft ohne Netz."""
import json
import os
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "router"))
from skirnir_router import cloud, proxy   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def err(status, body):
    return cloud.CloudError(status, *cloud._err_info(json.dumps(body).encode() if not isinstance(body, bytes) else body))


openai_404 = {"error": {"message": "The model `gpt-x` does not exist or you do not have access to it.", "type": "invalid_request_error",
                        "param": None, "code": "model_not_found"}}
e = err(404, openai_404)
check("OpenAI 404 model_not_found -> model_unavailable, kein Client-Fehler (naechster Kandidat)",
      e.code == "model_not_found" and e.model_unavailable and not proxy.cloud_client_fault(e), f"{e.code} {e.model_unavailable}")
check("... Meldung bleibt fuer Log/Client erhalten", str(e).startswith("The model `gpt-x`"), str(e))

anthropic_404 = {"type": "error", "error": {"type": "not_found_error", "message": "model: claude-x"}}
e = err(404, anthropic_404)
check("Anthropic 404 not_found_error -> model_unavailable", e.code == "not_found_error" and not proxy.cloud_client_fault(e), e.code)

e = err(400, {"error": {"message": "The model does not support this parameter", "type": "invalid_request_error", "code": "unsupported_parameter"}})
check("400 mit 'The model ...' im Text, aber ohne Modell-Code -> Client-Fehler (der Text zaehlt nicht mehr)",
      proxy.cloud_client_fault(e) and not e.model_unavailable, e.code)

e = err(404, {"error": {"message": "Unknown URL", "type": "invalid_request_error", "code": "unknown_url"}})
check("404 mit anderem Code -> Client-Fehler", proxy.cloud_client_fault(e))

e = err(500, {"error": {"message": "The server had an error", "type": "server_error"}})
check("500 -> kein Client-Fehler (naechster Kandidat, wie bisher)", not proxy.cloud_client_fault(e))

e = err(404, b"<html>not found</html>")
check("Kein JSON -> Text bleibt Meldung, Code leer, 404 = Client-Fehler", e.code == "" and str(e).startswith("<html>") and proxy.cloud_client_fault(e))

e = err(429, {"error": {"message": "Rate limit", "type": "requests", "code": "rate_limit_exceeded"}})
check("429 -> kein Client-Fehler (nicht in CLIENT_FAULT_STATUSES)", not proxy.cloud_client_fault(e))

e = cloud.CloudError(500, "x", code="model_not_found")
check("model_not_found zaehlt nur bei 400/403/404", not e.model_unavailable)

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
