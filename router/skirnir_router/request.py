"""Stage 1 (design/roadmap.md): Request semantics - the client describes, the router decides.

A request optionally carries a `routing` block, in the same form natively (/api/chat, /api/generate, /api/embed) and
OpenAI-compatible (/v1/*). It is NOT forwarded to Ollama.

    routing:
      require: [tools, vision, thinking, structured, embedding, insert]  # Mandatory: tiers without these capabilities are excluded
      prefer:  [thinking]        # Preference: if a tier can serve this, only such tiers are considered
      min_context: 32768         # Lower bound for the tier's context (the context ladder remains the fit mechanism)
      session_id: "..."          # Affinity: same node + same model as last time, as long as it is warm there
      request_id: "..."          # mirrored in response (routing block), header X-Skirnir-Request-Id and decision log

The router also derives requirements from the request itself: tools -> tools, images -> vision, think -> thinking,
format as Schema/json -> structured, suffix -> insert (embedding only on explicit require). Capabilities come from Ollama's
/api/show (state.CAPS), supplemented by catalog overrides `models.<name>.capabilities: {structured: false, ...}` - Ollama
does not know `structured` as a capability, gpt-oss responds to a schema with HTTP 500.

Response: Headers X-Skirnir-Request-Id/-Node/-Model/-Tier/-Warm always; the `routing` block in the body only if the client
sent a `routing` block (legacy clients like the HA-Ollama integration see an unchanged body).
"""

import secrets
import time

from . import cloud, state
from .managed import parse as parse_session_request
from .memory_config import parse_memory_request
from .outcome import failed, invalid
from .common import DATA_CLASSES_DEFAULT, PRIORITIES

KNOWN_CAPS = ("completion", "tools", "vision", "thinking", "structured", "embedding", "insert")
# Ollama does not report these as capabilities; they apply unless the catalog says otherwise
DEFAULT_TRUE = {"structured", "completion"}
ROUTING_KEYS = {"require", "prefer", "min_context", "session_id", "request_id", "priority", "deadline_ms", "idempotency_key",
                "execution", "data_class", "memory", "session"}
EXECUTIONS = ("auto", "local", "cloud")   # Stage 5: auto = role order, local = opt-out, cloud = cloud tiers first
MAX_ID_LEN = 128
REQUEST_ID_BYTES = 6   # "skirnir-" + 12 hex chars: unique enough for logs and decision log, short enough to type


def new_request_id():
    return "skirnir-" + secrets.token_hex(REQUEST_ID_BYTES)


def overrides(model):
    twin = state.CFG.catalog_twin(model) or {}
    return twin.get("capabilities") or {}


def lacks(model, caps):
    """Which of the required capabilities does the model demonstrably lack? Unknown (no /api/show yet) does not block -
    the router only blocks what it knows; Ollama reports the rest itself."""
    raw = state.CAPS.get(model)
    over = overrides(model)
    missing = []
    for c in caps:
        if c in over:
            if not over[c]:
                missing.append(c)
            continue
        if c in DEFAULT_TRUE:
            continue
        if raw is None:
            continue
        if c not in raw:
            missing.append(c)
    return missing


def effective_caps(model):
    """For /admin/state: reported capabilities + catalog overrides; None until known."""
    raw = state.CAPS.get(model)
    over = overrides(model)
    if raw is None and not over:
        return None
    caps = set(raw or []) | DEFAULT_TRUE
    for c, v in over.items():
        (caps.add if v else caps.discard)(c)
    return sorted(caps)


def implicit_caps(body, path):
    caps = set()
    # /api/embed does NOT derive an 'embedding' requirement: Ollama only lists the capability for pure embedding models,
    # Chat models can still embed (and those that cannot report 501 - the router passes this through 1:1).
    if body.get("tools"):
        caps.add("tools")
    msgs = body.get("messages") or []
    if body.get("images") or any(isinstance(m, dict) and m.get("images") for m in msgs):
        caps.add("vision")
    think = body.get("think")
    if think is True or (isinstance(think, str) and think.lower() not in ("", "false", "none", "off")):
        caps.add("thinking")
    fmt = body.get("format")
    if isinstance(fmt, dict) or fmt == "json":
        caps.add("structured")
    if body.get("suffix"):
        caps.add("insert")
    return caps


def count_images(body):
    n = len(body.get("images") or [])
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            n += len(m.get("images") or [])
    return n


class Routing:
    @classmethod
    def fresh(cls):
        """Empty routing specs with a new request ID (internal requests without client body, e.g. shadow run)."""
        r = cls()
        r.request_id = new_request_id()
        return r

    def __init__(self):
        self.require = set()
        self.prefer = set()
        self.min_context = None
        self.session_id = None
        self.request_id = None
        self.explicit = False      # Client sent a routing block -> routing info in the body
        self.skipped = []          # [{"tier", "model", "ctx", "reason"}] of the last selection
        self.reason = None         # affinity | warm-first | rank
        self.candidates = []       # Stage 3: candidates of the chosen tier with score
        self.priority = None       # Stage 3: interactive | normal | batch (role as default, client truncates)
        self.deadline_ms = None    # Stage 3: longest wait for a slot
        self.queued_ms = 0
        self.canary = None         # Stage 6: model of the canary tier, if this request was selected
        self.execution = "auto"    # Stage 5
        self.data_class = None
        self.data_class_effective = None
        self.cloud_block = None    # Reason why this request cannot go to the cloud (applies to all cloud tiers)
        self.session = None
        self.memory_context = None
        self.memory_items = []
        self.memory_state = None   # State of the recall (memory.Recall.state), as header X-Skirnir-Memory-State
        self.context_sensitive = False
        self.memory = None
        self.memory_scope = None
        self.decision = None       # Decision Engine: result (as_dict), if the auto role was requested

    def tier_blocked(self, tier):
        """Reason why this tier is not suitable for the request - or None."""
        if tier.get("cloud"):
            why = self.cloud_block or cloud.provider_block_reason(tier, self)
            if why:
                return why
        if self.min_context and tier["num_ctx"] < self.min_context:
            return f"num_ctx {tier['num_ctx']} < min_context {self.min_context}"
        missing = lacks(tier["model"], sorted(self.require))
        if missing:
            return "missing capability: " + ", ".join(missing)
        return None

    def info(self, role, tier_idx, tier, ctx, node, warm, via, client=None):
        d = {"request_id": self.request_id, "role": role["name"], "model": tier["model"], "node": node.name,
             "tier": tier_idx, "ctx": ctx, "warm": warm, "via": via, "reason": self.reason,
             "node_state": node.state, "skipped": list(self.skipped)}
        if self.session_id:
            d["session_id"] = self.session_id
        if client:
            d["client"] = client
        if self.require:
            d["required"] = sorted(self.require)
        d["candidates"] = list(self.candidates)
        d["priority"] = self.priority
        d["queued_ms"] = round(self.queued_ms)
        d["canary"] = bool(self.canary) and tier["model"] == self.canary
        if self.decision:
            d["decision"] = self.decision
        d["cloud"] = bool(tier.get("cloud"))
        d["execution"] = self.execution
        if self.data_class_effective:
            d["data_class"] = self.data_class_effective
        if self.memory_state:
            d["memory_state"] = self.memory_state
        return d


def parse(body, path, headers=None):
    """Take the routing block from the body and check it, derive requirements. Returns Routing or a Problem 400 with
    plain text (until 0.8.8 a ValueError caught by proxy._prepare)."""
    rb = body.pop("routing", None)
    r = Routing()
    if rb is not None:
        if not isinstance(rb, dict):
            return invalid("routing must be an object")
        unknown = set(rb) - ROUTING_KEYS
        if unknown:
            return invalid(f"routing: unknown field(s) {', '.join(sorted(unknown))}; known: {', '.join(sorted(ROUTING_KEYS))}")
        for key in ("require", "prefer"):
            vals = rb.get(key) or []
            if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                return invalid(f"routing.{key} must be a list of capability names")
            bad = [v for v in vals if v not in KNOWN_CAPS]
            if bad:
                return invalid(f"routing.{key}: unknown capability {', '.join(bad)}; known: {', '.join(KNOWN_CAPS)}")
            setattr(r, key, set(vals))
        mc = rb.get("min_context")
        if mc is not None:
            if not isinstance(mc, int) or isinstance(mc, bool) or mc <= 0:
                return invalid("routing.min_context must be a positive integer")
            r.min_context = mc
        for key in ("session_id", "request_id"):
            v = rb.get(key)
            if v is not None:
                v = str(v)
                if not v or len(v) > MAX_ID_LEN:
                    return invalid(f"routing.{key} must be 1-{MAX_ID_LEN} characters")
                setattr(r, key, v)
        pr = rb.get("priority")
        if pr is not None:
            if pr not in PRIORITIES:
                return invalid(f"routing.priority must be one of {', '.join(PRIORITIES)}")
            r.priority = pr
        dl = rb.get("deadline_ms")
        if dl is not None:
            if not isinstance(dl, int) or isinstance(dl, bool) or dl <= 0:
                return invalid("routing.deadline_ms must be a positive integer")
            r.deadline_ms = dl
        ex = rb.get("execution")
        if ex is not None:
            if ex not in EXECUTIONS:
                return invalid(f"routing.execution must be one of {', '.join(EXECUTIONS)}")
            r.execution = ex
        dc = rb.get("data_class")
        if dc is not None:
            classes = state.CFG.cloud.get("data_classes") or DATA_CLASSES_DEFAULT
            if dc not in classes:
                return invalid(f"routing.data_class must be one of {', '.join(classes)}")
            r.data_class = dc
        r.session = parse_session_request(rb.get("session"))
        r.memory = parse_memory_request(rb.get("memory"))
        for parsed in (r.session, r.memory):
            if failed(parsed):
                return invalid(parsed.message)
        r.explicit = True
    r.require |= implicit_caps(body, path)
    if not r.request_id:
        hdr = (headers or {}).get("X-Request-Id") if headers is not None else None
        r.request_id = str(hdr)[:MAX_ID_LEN] if hdr else new_request_id()
    return r


def check_limits(body):
    """Request size limits (router.limits) - 413 plain text or None. Coarse, but before the backend."""
    lim = state.CFG.limits
    n = count_images(body)
    if n > lim["max_images"]:
        return f"too many images ({n} > {lim['max_images']})"
    n = len(body.get("tools") or [])
    if n > lim["max_tools"]:
        return f"too many tools ({n} > {lim['max_tools']})"
    n = len(body.get("messages") or [])
    if n > lim["max_messages"]:
        return f"too many messages ({n} > {lim['max_messages']})"
    return None


def headers_for(info):
    h = {"X-Skirnir-Request-Id": str(info["request_id"]), "X-Skirnir-Node": info["node"], "X-Skirnir-Model": info["model"],
         "X-Skirnir-Tier": str(info["tier"]), "X-Skirnir-Warm": "1" if info["warm"] else "0"}
    if info.get("memory_state"):   # only for recall: a failure (even under fail_open) never looks like "no knowledge"
        h["X-Skirnir-Memory-State"] = info["memory_state"]
    return h


# --- Session affinity (small, §10): same node + same model, as long as warm; entries expire ---

def affinity(session_id):
    e = state.SESSIONS.get(session_id)
    if not e:
        return None
    if time.time() - e["t"] > state.CFG.session_ttl_s:
        state.SESSIONS.pop(session_id, None)
        return None
    return e


def remember_session(session_id, node_name, model):
    if not session_id:
        return
    now = time.time()
    state.SESSIONS[session_id] = {"node": node_name, "model": model, "t": now}
    if len(state.SESSIONS) > 5000:   # Cleanup: remove expired, oldest first in case of doubt
        for k in sorted(state.SESSIONS, key=lambda k: state.SESSIONS[k]["t"])[:1000]:
            state.SESSIONS.pop(k, None)
