#!/usr/bin/env python3
"""Self-test (Router 0.8.9): Provider errors are distinguished by structured error code, never by message text.

Up to 0.8.9, proxy._via_cloud used str(e).lower().startswith("the model") to decide whether a provider 4xx goes to the client
or if another candidate can help. Now CloudError carries the code from the error body (OpenAI error.code,
Anthropic error.type) and model_unavailable; proxy.cloud_client_fault decides based on that. Runs without network."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
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
check("OpenAI 404 model_not_found -> model_unavailable, not a client error (next candidate)",
      e.code == "model_not_found" and e.model_unavailable and not proxy.cloud_client_fault(e), f"{e.code} {e.model_unavailable}")
check("... message remains for log/client", str(e).startswith("The model `gpt-x`"), str(e))

anthropic_404 = {"type": "error", "error": {"type": "not_found_error", "message": "model: claude-x"}}
e = err(404, anthropic_404)
check("Anthropic 404 not_found_error -> model_unavailable", e.code == "not_found_error" and not proxy.cloud_client_fault(e), e.code)

e = err(400, {"error": {"message": "The model does not support this parameter", "type": "invalid_request_error", "code": "unsupported_parameter"}})
check("400 with 'The model ...' in text, but no model code -> client error (text no longer counts)",
      proxy.cloud_client_fault(e) and not e.model_unavailable, e.code)

e = err(404, {"error": {"message": "Unknown URL", "type": "invalid_request_error", "code": "unknown_url"}})
check("404 with different code -> client error", proxy.cloud_client_fault(e))

e = err(500, {"error": {"message": "The server had an error", "type": "server_error"}})
check("500 -> not a client error (next candidate, as before)", not proxy.cloud_client_fault(e))

e = err(404, b"<html>not found</html>")
check("No JSON -> text remains message, code empty, 404 = client error", e.code == "" and str(e).startswith("<html>") and proxy.cloud_client_fault(e))

e = err(429, {"error": {"message": "Rate limit", "type": "requests", "code": "rate_limit_exceeded"}})
check("429 -> not a client error (not in CLIENT_FAULT_STATUSES)", not proxy.cloud_client_fault(e))

e = cloud.CloudError(500, "x", code="model_not_found")
check("model_not_found only counts for 400/403/404", not e.model_unavailable)

print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
