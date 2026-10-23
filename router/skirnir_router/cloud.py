"""Stage 5 (design/roadmap.md): Cloud providers as tiers in roles - local first, cloud only where the role allows it.

Operator decision (2026-09-10): no per-client opt-in; instead, the router defines in roles where cloud can be a tier
(`tiers: [{model: "openai:gpt-5-mini", num_ctx: 128000}]`). Cloud is used when (1) no local node
can (both machines busy, gpu-desktop off), (2) a capability is missing locally (vision, structured, ...: local tiers fail
`require`) or (3) the client explicitly requests it (`routing.execution: cloud` moves cloud tiers forward).
The client can protect itself: `routing.execution: local` or `client_auth.clients.<c>.cloud: false` (opt-out).

Constraints, all configurable (`router.cloud`):
- Data classes as DECLARATION (no content classifier): `routing.data_class`, else client default, else
  `default_data_class` (personal). Cloud only if class is at most `max_cloud_data_class` (internal).
- Credential scan: prompts with recognizable keys/passwords do not go to cloud (`credential_scan: block`).
- Budget per provider and month in CHF (prices per model in catalog); exhausted = provider is no longer a tier; warning
  at `warn_at_percent` as HA Problem.
- Egress: only hosts of configured providers (allowlist), keys from secrets.env (Secret-Store) - never in log.
- Circuit breaker like for nodes.

Adapters: `openai` (also for OpenAI-compatible endpoints like Gemini) and `anthropic`. Both translate the native
Ollama request (Chat, Generate, Embed; Tools, Images, format, Stream) to the provider API and the response back to
Ollama format - then the same paths apply as for local nodes (Shapes for /v1, Routing-Info, Metrics, Usage).
"""

import json
import re
import time
from collections import Counter

from aiohttp import ClientTimeout

from . import state
from .common import DATA_CLASSES_DEFAULT, log, parse_tool_args, read_env_value
from .nodes import Breaker

DEFAULT_HOSTS = {"api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com"}
SECRET_PATTERNS = [
    ("openai/anthropic key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("password/secret assignment", re.compile(r"(?i)\b(passwor[dt]|passwd|api[_-]?key|client[_-]?secret|secret[_-]?key|access[_-]?token)\b\s*[:=]\s*\S{8,}")),
]


class CloudTarget(Breaker):
    """A cloud provider in the role of a node: same methods used by Scheduler/Proxy on a Node."""

    is_cloud = True
    speaks_openai = False  # a node attribute (openai_node.py); the cloud path is chosen by is_cloud first
    guard_policy = False   # no GPU guard in the cloud (scheduler.score does not check further)

    def __init__(self, provider, spec):
        self.provider = provider
        self.name = f"cloud:{provider}"
        self.spec = spec
        self.state = "free"
        self.inflight = 0
        self.inflight_models = Counter()
        self.last_used = {}
        self.loaded = {}
        self.models = set()
        self.weight = 1
        self.vram_free_gib = None
        self.vram_total_gib = 0.0
        self.gpu_util = None
        self.breaker = "closed"
        self.breaker_fails = []
        self.breaker_until = 0.0
        self.api_key = None
        self.key_source = None

    # --- Node-Schnittstelle ---
    def is_loaded(self, model):
        return False

    def loaded_context(self, model):
        return None

    def same_blob(self, a, b):
        return a == b

    def gpu_known(self, now):
        return False

    def announce_load(self, model, ctx):
        pass

    def finish_load(self, model):
        pass

    def budget_gib(self, now, model):
        return float("inf")

    def effective_max_inflight(self):
        return int(self.spec.get("max_inflight", 8))

    def snapshot(self):
        spend = spend_month(self.provider)
        budget = float(self.spec.get("budget_month_chf") or 0)
        return {"provider": self.provider, "kind": self.spec["kind"], "enabled": bool(self.spec.get("enabled", True)),
                "key": bool(self.api_key), "key_source": self.key_source, "base_url": self.spec["base_url"], "region": self.spec.get("region"),
                "models": sorted(self.models), "inflight": self.inflight, "breaker": self.breaker,
                "spend_month_chf": round(spend, 4), "budget_month_chf": budget,
                "budget_percent": round(100 * spend / budget, 1) if budget else None, "max_data_class": self.spec.get("max_data_class")}


# ---------------- Setup from configuration ----------------

def setup():
    """After reading configuration: create/update targets, load keys (never log)."""
    cfg = state.CFG.cloud
    new = {}
    for name, spec in (cfg.get("providers") or {}).items():
        t = state.CLOUD.get(name) or CloudTarget(name, spec)
        t.spec = spec
        t.models = {m for m, e in state.CFG.models.items() if (e or {}).get("cloud") == name}
        t.api_key, t.key_source = _load_key(spec)
        new[name] = t
        log.info("cloud provider %s (%s): %s, key %s, %d models, budget %s CHF/month", name, spec["kind"],
                 "active" if spec.get("enabled", True) else "disabled", f"from {t.key_source}" if t.api_key else "MISSING",
                 len(t.models), spec.get("budget_month_chf"))
    state.CLOUD = new


def _load_key(spec):
    import os
    env = spec.get("api_key_env")
    if not env:
        return None, None
    if os.environ.get(env):
        return os.environ[env], "env"
    path = spec.get("secrets_file")
    if path:
        try:
            v = read_env_value(path, env, strip_quotes=True)
            if v:
                return v, path
        except OSError as e:  # exception-ok: file system; the file can vanish or lose permissions between check and read
            log.warning("secrets_file %s: %s", path, e)
    return None, None


def target_for(tier):
    return state.CLOUD.get(tier.get("cloud") or "")


def class_index(name):
    classes = state.CFG.cloud.get("data_classes") or DATA_CLASSES_DEFAULT
    return classes.index(name) if name in classes else len(classes)


# ---------------- Schranken ----------------

def request_block_reason(req, body, client_name):
    """Why this REQUEST cannot go to cloud (independent of provider) - or None. Reported as reason at each
    cloud tier (`skipped`), local tiers continue normally."""
    cfg = state.CFG.cloud
    if not cfg.get("enabled"):
        return "cloud disabled in the configuration"
    if req.execution == "local":
        return "routing.execution: local (client opt-out)"
    cc = ((state.CFG.client_auth.get("clients") or {}).get(client_name or "") or {})
    if cc.get("cloud") is False:
        return f"client {client_name}: cloud: false (opt-out)"
    dc = req.data_class or cc.get("data_class") or cfg.get("default_data_class", "personal")
    req.data_class_effective = dc
    if class_index(dc) > class_index(cfg.get("max_cloud_data_class", "internal")):
        return f"data class {dc} > {cfg.get('max_cloud_data_class', 'internal')} (not cleared for the cloud)"
    if cfg.get("credential_scan", "block") == "block":
        hit = scan_secrets(body)
        if hit:
            return f"credential_scan: possible secret in the prompt ({hit})"
    return None


def provider_block_reason(tier, req=None):
    t = target_for(tier)
    if t is None:
        return f"cloud provider {tier.get('cloud')} not configured"
    if not t.spec.get("enabled", True):
        return f"cloud provider {t.provider} disabled"
    if not t.api_key:
        return f"cloud provider {t.provider}: no API key"
    if req is not None and t.spec.get("max_data_class"):
        dc = getattr(req, "data_class_effective", None) or state.CFG.cloud.get("default_data_class", "personal")
        if class_index(dc) > class_index(t.spec["max_data_class"]):
            return f"data class {dc} > {t.spec['max_data_class']} for provider {t.provider}"
    budget = float(t.spec.get("budget_month_chf") or 0)
    if budget and spend_month(t.provider) >= budget:
        return f"cloud provider {t.provider}: monthly budget {budget:.2f} CHF exhausted"
    return None


def _texts(body):
    out = []
    for m in body.get("messages") or []:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            out.append(m["content"])
    for k in ("prompt", "system"):
        if isinstance(body.get(k), str):
            out.append(body[k])
    inp = body.get("input")
    if isinstance(inp, str):
        out.append(inp)
    elif isinstance(inp, list):
        out += [x for x in inp if isinstance(x, str)]
    return out


def scan_secrets(body):
    for text in _texts(body):
        for name, rx in SECRET_PATTERNS:
            if rx.search(text):
                return name
    return None


# ---------------- Budget / Kosten ----------------

def month_key():
    return time.strftime("%Y-%m")


def spend_month(provider):
    return float(((state.USAGE.get("cloud") or {}).get(provider) or {}).get(month_key(), 0.0))


def cost_chf(model, prompt_tokens, completion_tokens):
    e = state.CFG.models.get(model) or {}
    p = e.get("price_chf_per_m") or {}
    return (prompt_tokens or 0) / 1e6 * float(p.get("input", 0)) + (completion_tokens or 0) / 1e6 * float(p.get("output", 0))

def add_spend(provider, chf):
    c = state.USAGE.setdefault("cloud", {}).setdefault(provider, {})
    c[month_key()] = round(c.get(month_key(), 0.0) + chf, 6)
    state.USAGE_DIRTY[0] = time.time()


def budget_view():
    out = {}
    for name, t in state.CLOUD.items():
        s = t.snapshot()
        out[name] = {k: s[k] for k in ("enabled", "key", "spend_month_chf", "budget_month_chf", "budget_percent", "breaker", "inflight")}
    return out


def budget_problems():
    probs = []
    for name, t in state.CLOUD.items():
        budget = float(t.spec.get("budget_month_chf") or 0)
        if not budget:
            continue
        pct = 100 * spend_month(name) / budget
        warn = float(t.spec.get("warn_at_percent", 80))
        if pct >= 100:
            probs.append(f"Cloud {name}: Monatsbudget erschoepft ({spend_month(name):.2f}/{budget:.2f} CHF)")
        elif pct >= warn:
            probs.append(f"Cloud {name}: {pct:.0f} % des Monatsbudgets ({spend_month(name):.2f}/{budget:.2f} CHF)")
    return probs


# ---------------- Uebersetzung Ollama -> Anbieter -> Ollama ----------------

def _model_id(model):
    e = state.CFG.models.get(model) or {}
    return e.get("provider_model") or model.split(":", 1)[1] if ":" in model else model


def _oa_messages(body, path):
    msgs = []
    if path == "/api/generate":
        if body.get("system"):
            msgs.append({"role": "system", "content": body["system"]})
        u = {"role": "user", "content": body.get("prompt", "")}
        if body.get("images"):
            u["content"] = [{"type": "text", "text": body.get("prompt", "")}] + [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}} for img in body["images"]]
        msgs.append(u)
        return msgs
    call_ids = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        o = {"role": role}
        content = m.get("content") or ""
        if m.get("images"):
            o["content"] = [{"type": "text", "text": content}] + [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}} for img in m["images"]]
        else:
            o["content"] = content
        if role == "assistant" and m.get("tool_calls"):
            tcs = []
            for _i, tc in enumerate(m["tool_calls"]):
                fn = (tc or {}).get("function") or {}
                cid = tc.get("id") or f"call_{len(call_ids) + 1}"
                call_ids.append(cid)
                args = fn.get("arguments", {})
                tcs.append({"id": cid, "type": "function", "function": {"name": fn.get("name", ""), "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
            o["tool_calls"] = tcs
            if not content:
                o["content"] = None
        if role == "tool":
            o["tool_call_id"] = m.get("tool_call_id") or (call_ids.pop(0) if call_ids else "call_1")
        msgs.append(o)
    return msgs


def _oa_body(body, path, model_id, stream):
    oa = {"model": model_id, "messages": _oa_messages(body, path), "stream": stream}
    if body.get("tools"):
        oa["tools"] = body["tools"]
    fmt = body.get("format")
    if fmt == "json":
        oa["response_format"] = {"type": "json_object"}
    elif isinstance(fmt, dict):
        oa["response_format"] = {"type": "json_schema", "json_schema": {"name": "antwort", "schema": fmt}}
    opts = body.get("options") or {}
    if opts.get("temperature") is not None:
        oa["temperature"] = opts["temperature"]
    if opts.get("top_p") is not None:
        oa["top_p"] = opts["top_p"]
    if opts.get("seed") is not None:
        oa["seed"] = opts["seed"]
    if opts.get("stop"):
        oa["stop"] = opts["stop"]
    if opts.get("num_predict") and int(opts["num_predict"]) > 0:
        oa["max_completion_tokens"] = int(opts["num_predict"])
    if stream:
        oa["stream_options"] = {"include_usage": True}
    # Ollama `think` -> OpenAI reasoning_effort, only for models marked as reasoning in catalog (gpt-5, o-series):
    # think: false = minimal (HA-AI-Tasks; otherwise gpt-5-mini thinks 130 tokens for an "OK"), think: true = provider default.
    e = state.CFG.models.get(oa_model_key(model_id)) or {}
    if body.get("think") is False and e.get("reasoning"):
        oa["reasoning_effort"] = "minimal"
    return oa


def oa_model_key(model_id):
    return next((k for k, v in state.CFG.models.items() if (v or {}).get("cloud") and _model_id(k) == model_id), None)


def _oa_tool_calls_to_native(tcs):
    out = []
    for tc in tcs or []:
        fn = (tc or {}).get("function") or {}
        out.append({"id": (tc or {}).get("id"), "function": {"name": fn.get("name", ""), "arguments": parse_tool_args(fn.get("arguments", "{}"))}})
    return out


def _native_done(model, path, content, tool_calls, finish, p_tok, c_tok, t0):
    now = time.time()
    dur = int((now - t0) * 1e9)
    j = {"model": model, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "done": True,
         "done_reason": "length" if finish in ("length", "max_tokens") else "stop",
         "prompt_eval_count": p_tok or 0, "eval_count": c_tok or 0, "prompt_eval_duration": 0, "eval_duration": max(1, dur),
         "load_duration": 0, "total_duration": dur}
    if path == "/api/generate":
        j["response"] = content or ""
    else:
        msg = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        j["message"] = msg
    return j


# ---- Anthropic ----

def _an_body(body, path, model_id, stream):
    system, msgs = [], []
    if path == "/api/generate":
        if body.get("system"):
            system.append(body["system"])
        parts = [{"type": "text", "text": body.get("prompt", "")}]
        parts += [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}} for img in body.get("images") or []]
        msgs.append({"role": "user", "content": parts})
    else:
        pending_ids = []
        for m in body.get("messages") or []:
            if not isinstance(m, dict):
                continue
            role, content = m.get("role", "user"), m.get("content") or ""
            if role == "system":
                system.append(content)
                continue
            if role == "tool":
                cid = m.get("tool_call_id") or (pending_ids.pop(0) if pending_ids else "toolu_1")
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": cid, "content": content}]})
                continue
            parts = []
            if content:
                parts.append({"type": "text", "text": content})
            for img in m.get("images") or []:
                parts.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}})
            if role == "assistant":
                for _i, tc in enumerate(m.get("tool_calls") or []):
                    fn = (tc or {}).get("function") or {}
                    cid = tc.get("id") or f"toolu_{len(pending_ids) + 1}"
                    pending_ids.append(cid)
                    parts.append({"type": "tool_use", "id": cid, "name": fn.get("name", ""), "input": parse_tool_args(fn.get("arguments", {}))})
            if not parts:
                parts = [{"type": "text", "text": " "}]
            if msgs and msgs[-1]["role"] == role:   # Anthropic verlangt abwechselnde Rollen
                msgs[-1]["content"] += parts
            else:
                msgs.append({"role": role, "content": parts})
    opts = body.get("options") or {}
    an = {"model": model_id, "messages": msgs, "stream": stream,
          "max_tokens": int(opts["num_predict"]) if opts.get("num_predict") and int(opts["num_predict"]) > 0 else 4096}
    if system:
        an["system"] = "\n\n".join(system)
    if body.get("tools"):
        an["tools"] = [{"name": (t.get("function") or {}).get("name", ""), "description": (t.get("function") or {}).get("description", ""),
                        "input_schema": (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}}
                       for t in body["tools"] if isinstance(t, dict)]
    if opts.get("temperature") is not None:
        an["temperature"] = opts["temperature"]
    if opts.get("top_p") is not None:
        an["top_p"] = opts["top_p"]
    if opts.get("stop"):
        an["stop_sequences"] = opts["stop"] if isinstance(opts["stop"], list) else [opts["stop"]]
    return an


def _an_content_to_native(blocks):
    text, tcs = "", []
    for b in blocks or []:
        if b.get("type") == "text":
            text += b.get("text", "")
        elif b.get("type") == "tool_use":
            tcs.append({"id": b.get("id"), "function": {"name": b.get("name", ""), "arguments": b.get("input") or {}}})
    return text, tcs


def _headers(target):
    if target.spec["kind"] == "anthropic":
        return {"x-api-key": target.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    return {"Authorization": f"Bearer {target.api_key}", "content-type": "application/json"}


def _url(target, path, model):
    base = target.spec["base_url"].rstrip("/")
    if target.spec["kind"] == "anthropic":
        return base + "/v1/messages"
    if path in ("/api/embed", "/api/embeddings"):
        return base + "/embeddings"
    return base + "/chat/completions"


async def _sse(resp):
    """SSE-Zeilen -> JSON-Objekte (data: ...), [DONE] beendet."""
    buf = b""
    async for chunk in resp.content.iter_any():
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:  # exception-ok: provider data; json.loads is the only full validity check
                continue


class CloudError(Exception):
    """A provider's HTTP error. A class, not a return value, because the streaming answer is an async generator: its first
    read raises at the caller, which handles stream and unary errors in one place (proxy._via_cloud).
    model_unavailable: the provider says the model does not exist or this key may not use it (OpenAI error.code
    model_not_found, Anthropic 404 not_found_error) - another candidate may serve, so it is no client fault. Taken from the
    provider's structured error, never from its message text (until 0.8.8 proxy.py checked for "the model ..." in it)."""

    def __init__(self, status, msg, code=None):
        super().__init__(msg)
        self.status = status
        self.code = code or ""
        self.model_unavailable = self.code in ("model_not_found", "not_found_error") and status in (400, 403, 404)


async def run(target, path, body, model, stream):
    """Request to provider. Returns (info, chunks): info = {"p_tok","c_tok","finish"} filled after completion,
    chunks = async generator of Ollama chunks (if stream) or exactly one finished Ollama-JSON (not stream)."""
    model_id = _model_id(model)
    kind = target.spec["kind"]
    t0 = time.time()
    timeout = ClientTimeout(total=float(target.spec.get("timeout_s", state.CFG.request_timeout_s)), sock_connect=10)
    if path in ("/api/embed", "/api/embeddings"):
        if kind == "anthropic":
            raise CloudError(501, f"{target.provider}: no embeddings")  # exception-ok: same signal as a provider error (CloudError)
        inp = body.get("input") if "input" in body else body.get("prompt")
        req = {"model": model_id, "input": inp}
        async with state.SESSION.post(_url(target, path, model), json=req, headers=_headers(target), timeout=timeout) as r:
            data = await r.read()
            if r.status >= 400:
                raise CloudError(r.status, *_err_info(data))  # exception-ok: provider HTTP error (CloudError)
            j = json.loads(data)
        embs = [d.get("embedding") for d in sorted(j.get("data") or [], key=lambda d: d.get("index", 0))]
        p_tok = int((j.get("usage") or {}).get("prompt_tokens") or 0)
        out = {"model": model, "embeddings": embs, "prompt_eval_count": p_tok, "total_duration": int((time.time() - t0) * 1e9), "load_duration": 0}
        return {"p_tok": p_tok, "c_tok": 0, "finish": "stop"}, out

    if kind != "anthropic":
        def post(req):
            return state.SESSION.post(_url(target, path, model), json=req, headers=_headers(target), timeout=timeout)
        return await openai_exchange(post, path, body, model, model_id, stream)

    req = _an_body(body, path, model_id, stream)
    info = {"p_tok": 0, "c_tok": 0, "finish": "stop"}
    if not stream:
        async with state.SESSION.post(_url(target, path, model), json=req, headers=_headers(target), timeout=timeout) as r:
            data = await r.read()
            if r.status >= 400:
                raise CloudError(r.status, *_err_info(data))  # exception-ok: provider HTTP error (CloudError)
            j = json.loads(data)
        content, tcs = _an_content_to_native(j.get("content"))
        u = j.get("usage") or {}
        info.update(p_tok=int(u.get("input_tokens") or 0), c_tok=int(u.get("output_tokens") or 0), finish=j.get("stop_reason") or "end_turn")
        return info, _native_done(model, path, content, tcs, info["finish"], info["p_tok"], info["c_tok"], t0)

    async def gen():
        async with state.SESSION.post(_url(target, path, model), json=req, headers=_headers(target), timeout=timeout) as r:
            if r.status >= 400:   # inside the streaming generator: raising is its only error channel (CloudError)
                raise CloudError(r.status, *_err_info(await r.read()))  # exception-ok: provider HTTP error in the stream
            tool_blocks, tool_args = {}, {}
            async for ev in _sse(r):
                et = ev.get("type")
                if et == "message_start":
                    info["p_tok"] = int(((ev.get("message") or {}).get("usage") or {}).get("input_tokens") or 0)
                elif et == "content_block_start":
                    cb = ev.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        tool_blocks[ev.get("index")] = {"id": cb.get("id"), "name": cb.get("name")}
                        tool_args[ev.get("index")] = ""
                elif et == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        yield {"model": model, **({"response": d["text"]} if path == "/api/generate" else {"message": {"role": "assistant", "content": d["text"]}}), "done": False}
                    elif d.get("type") == "input_json_delta":
                        tool_args[ev.get("index")] = tool_args.get(ev.get("index"), "") + (d.get("partial_json") or "")
                elif et == "message_delta":
                    info["finish"] = (ev.get("delta") or {}).get("stop_reason") or info["finish"]
                    info["c_tok"] = int((ev.get("usage") or {}).get("output_tokens") or info["c_tok"])
            tcs = []
            for idx, tb in tool_blocks.items():
                tcs.append({"id": tb["id"], "function": {"name": tb["name"], "arguments": parse_tool_args(tool_args.get(idx) or "{}")}})
        yield _native_done(model, path, "", tcs, info["finish"], info["p_tok"], info["c_tok"], t0)

    return info, gen()


async def openai_exchange(post, path, body, model, model_id, stream, max_tokens_field=False):
    """One chat/generate exchange with an OpenAI-compatible endpoint: the native Ollama request as /chat/completions,
    the answer back in Ollama form. Shared by cloud providers (run) and OpenAI-compatible nodes (openai_node.run).
    `post(req)` sends the JSON request and returns an async context manager with the response - aiohttp's for a
    provider, the tunnel's (nodes.nreq) for a node. Returns (info, chunks) like run(); HTTP errors raise CloudError.
    max_tokens_field: also send the older `max_tokens`, which local servers often know instead of max_completion_tokens."""
    t0 = time.time()
    req = _oa_body(body, path, model_id, stream)
    if max_tokens_field and req.get("max_completion_tokens"):
        req["max_tokens"] = req["max_completion_tokens"]
    info = {"p_tok": 0, "c_tok": 0, "finish": "stop"}
    if not stream:
        async with post(req) as r:
            data = await r.read()
            if r.status >= 400:
                raise CloudError(r.status, *_err_info(data))  # exception-ok: endpoint HTTP error (CloudError)
            j = json.loads(data)
        ch = (j.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        content, tcs = msg.get("content") or "", _oa_tool_calls_to_native(msg.get("tool_calls"))
        u = j.get("usage") or {}
        info.update(p_tok=int(u.get("prompt_tokens") or 0), c_tok=int(u.get("completion_tokens") or 0), finish=ch.get("finish_reason") or "stop")
        return info, _native_done(model, path, content, tcs, info["finish"], info["p_tok"], info["c_tok"], t0)

    async def gen():
        async with post(req) as r:
            if r.status >= 400:   # inside the streaming generator: raising is its only error channel (CloudError)
                raise CloudError(r.status, *_err_info(await r.read()))  # exception-ok: endpoint HTTP error in the stream
            partial = {}
            async for ev in _sse(r):
                if ev.get("usage"):
                    u = ev["usage"]
                    info["p_tok"], info["c_tok"] = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        yield {"model": model, **({"response": d["content"]} if path == "/api/generate" else {"message": {"role": "assistant", "content": d["content"]}}), "done": False}
                    for tc in d.get("tool_calls") or []:
                        p = partial.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                        p["id"] = tc.get("id") or p["id"]
                        fn = tc.get("function") or {}
                        p["name"] += fn.get("name") or ""
                        p["args"] += fn.get("arguments") or ""
                    if ch.get("finish_reason"):
                        info["finish"] = ch["finish_reason"]
            tcs = []
            for idx in sorted(partial):
                p = partial[idx]
                tcs.append({"id": p["id"], "function": {"name": p["name"], "arguments": parse_tool_args(p["args"])}})
        yield _native_done(model, path, "", tcs, info["finish"], info["p_tok"], info["c_tok"], t0)

    return info, gen()


def _err_info(data):
    """(message, code) of a provider error body. code = OpenAI error.code, else error.type (Anthropic), else "" - the
    structured part a caller may decide on; the message is for logs and the client only."""
    try:
        j = json.loads(data)
    except ValueError:  # exception-ok: provider data; json.loads is the only full validity check
        return data.decode(errors="replace")[:300], ""
    e = j.get("error") if isinstance(j, dict) else None
    if isinstance(e, dict):
        code = e.get("code") if isinstance(e.get("code"), str) else e.get("type") if isinstance(e.get("type"), str) else ""
        return str(e.get("message") or e)[:300], code
    return str(e or j)[:300], ""


def view():
    return {name: t.snapshot() for name, t in state.CLOUD.items()}
