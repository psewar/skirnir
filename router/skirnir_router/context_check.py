"""Pre-check of context: does a request fit into num_ctx at all?

Ollama silently truncates overly long prompts. In chat, its Go layer removes whole messages from the front before the prompt
reaches llama-server - this only appears in the log with OLLAMA_DEBUG (Evidence 2026-09-24 02:29:19: 350k characters arrived as
`task.n_tokens = 2477`, without a single log line). The client receives a normal response, just missing part of the history. The node agent
does not see this; the router sees both: what was sent and what arrived according to Ollama (prompt_eval_count = full prompt length, even with cached prefix).

Two tiers, so a rough estimate does not trigger a false alarm:
  before  - Estimate "every digit 1 token, rest Bytes/3.5" (Qwen splits numbers into single digits). Measured against 80
            real agent texts (tool outputs, user messages) vs qwen3.6: real/estimate median 0.99,
            p90 1.10, max 1.26. If it exceeds
            num_ctx, the request is a candidate.
  after   - If less than CONFIRMED x estimate arrived according to Ollama, truncation is confirmed: journal event
            `context_truncated`, metric, warning, HA event. If roughly the estimate arrived, only the estimate
            overshot - no alarm, just an info line (calibration aid).
Cloud tiers are excluded: providers reject overly long prompts with an error instead of silently truncating.
"""
import json

from . import state
from .common import log

BYTES_PER_TOKEN = 3.5
MESSAGE_OVERHEAD = 4   # Roles and separators of the chat template per message
CONFIRMED = 0.8          # arrived < 0.8 x estimated -> likely truncated
CERTAIN = 0.45             # arrived < 0.45 x estimated -> definitely truncated (worst overestimation measured: 0.46)
# Bounds, measured 2026-09-24 on 80 real texts (real/estimate): p10 0.85, min 0.46 - 4 of 80 were below 0.8,
# all with lots of whitespace (indented outputs; whitespace sequences are nearly free for the tokenizer). Finer rules
# (counting whitespace separately) only won in the sample, not in 5-fold cross-validation. Consequence: a
# truncation of less than ~25% is indistinguishable from an estimation error and remains invisible.
_DIGITS = "0123456789"


def estimate_text(text):
    """Tokens eines Texts: ASCII-Ziffern je 1, alles andere ceil(UTF-8-Bytes / 3,5)."""
    if not text:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))
    digits = sum(map(text.count, _DIGITS))
    rest = len(text.encode("utf-8", "replace")) - digits
    return digits + int(-(-rest // BYTES_PER_TOKEN))


def _upper_bound(body):
    """Cheap upper bound: no byte becomes more than one token (digits exactly 1, CJK 3 bytes per token)."""
    n = sum(len(body.get(k) or "") for k in ("system", "prompt", "suffix") if isinstance(body.get(k), str))
    for m in body.get("messages") or []:
        c = m.get("content")
        n += MESSAGE_OVERHEAD + (len(c.encode("utf-8", "replace")) if isinstance(c, str) else len(json.dumps(c or "")))
        if m.get("tool_calls"):
            n += len(json.dumps(m["tool_calls"]))
    if body.get("tools"):
        n += len(json.dumps(body["tools"]))
    return n


def estimate_body(body):
    """Estimated prompt tokens of an Ollama body (/api/chat or /api/generate). Images do not count -
    their cost depends on the model and is not in the bytes."""
    n = sum(estimate_text(body.get(k)) for k in ("system", "prompt", "suffix"))
    for m in body.get("messages") or []:
        n += MESSAGE_OVERHEAD + estimate_text(m.get("content"))
        if m.get("tool_calls"):
            n += estimate_text(m["tool_calls"])
    if body.get("tools"):
        n += estimate_text(body["tools"])
    return n


def before_request(body, ctx, is_cloud=False):
    """Estimate if the request might exceed num_ctx, else None (also for cloud/no ctx)."""
    if is_cloud or not ctx:
        return None
    if _upper_bound(body) < ctx:
        return None   # fits safely - the vast majority of requests end here without estimation
    est = estimate_body(body)
    return est if est > ctx else None


def after_request(estimated, received, ctx, info):
    """Comparison after the response. Returns True if truncation was confirmed and reported."""
    if not estimated or not received:
        return False
    if received >= CONFIRMED * estimated:
        log.info("context check: estimated %d > num_ctx %d, received %d - estimate too high, no truncation (%s)",
                 estimated, ctx, received, info.get("model"))
        return False
    ratio = received / estimated
    e = {"event": "context_truncated", "role": info.get("role"), "model": info.get("model"), "node": info.get("node"),
         "client": info.get("client"), "path": info.get("path"), "num_ctx": ctx, "estimated": estimated,
         "received": received, "ratio": round(ratio, 2), "certain": ratio < CERTAIN, "request_id": info.get("request_id")}
    log.warning("context %s truncated: %s sent ~%d tokens to %s (%s, num_ctx %d), received %d (%.0f %%) - Ollama truncates silently (req=%s)",
                "certainly" if e["certain"] else "probably", e["client"] or "-", estimated, e["model"], e["node"], ctx,
                received, ratio * 100, e["request_id"])
    state.remember(e)
    state.TRUNCATIONS["count"] += 1
    state.TRUNCATIONS["last"] = {k: v for k, v in e.items() if k not in ("event", "t")}
    state.MQTT_EVENTS.append({"event_type": "context_truncated", **state.TRUNCATIONS["last"]})
    del state.MQTT_EVENTS[:-20]
    return True


def conservative_bound(body):
    """Reusable text/tool byte bound; excludes model-specific image token costs."""
    return _upper_bound(body)
