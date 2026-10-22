"""Password hashes and Basic Auth for the control plane; client authentication for the API port (stage 2)."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections import deque

from aiohttp import web

from . import state
from .common import anthropic_error, basic_credentials, log, ollama_error, openai_error
from .outcome import Problem


# ----------------------------------------------------------------------------- Client-Authentifizierung (11434)
# Who may call the router is defined in config.yaml under router.client_auth. Three identification methods, in this
# Reihenfolge: "Authorization: Bearer <token>" (HA-Ollama-Integration, OpenAI-Clients), "Authorization: Basic
# <client>:<token>" (clients that only support Basic), source IP (clients that cannot send any header -
# node-red-contrib-ollama sends its API key only to ollama.com). Tokens are random and long, so
# sha256 instead of PBKDF2: the check runs on every request; a slow hash would be self-sabotage here.
# A wrong token does NOT fall back to IP - it is an error, not anonymity.
EXEMPT_PATHS = {"/", "/api/version"}   # Health checks remain accessible without identity (smoke test, monitoring)
_UNAUTH_AUDIT_EVERY_S = 300            # at most one audit line per source IP in this time (otherwise a poller floods the log)
_audit_failed = [False]


def client_auth_cfg():
    return getattr(state.CFG, "client_auth", None) or {"mode": "observe", "clients": {}, "audit_log": None}


def client_auth_mode():
    return state.CLIENT_AUTH_MODE or client_auth_cfg().get("mode", "observe")


def audit(event, **fields):
    """Security-relevant event as a JSON line to the audit log (config: client_auth.audit_log) and the journal.
    No prompts, no tokens - only who, what, why. If writing fails, the router continues."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    line = json.dumps(rec, ensure_ascii=False)
    path = client_auth_cfg().get("audit_log")
    if path:
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:  # exception-ok: file system boundary; the event still goes to the journal below
            if not _audit_failed[0]:
                log.warning("audit log %s not writable (%s) - events only in the journal", path, e)
                _audit_failed[0] = True
    log.info("AUDIT %s", line)


def resolve_client(request):
    """(client name, method) or (None, reason). Method: bearer | basic | ip; reason: bad_token | None (nothing sent)."""
    clients = client_auth_cfg().get("clients") or {}
    header = request.headers.get("Authorization", "")
    token, user, key_via = None, None, "bearer"
    if header.startswith("Bearer "):
        token = header[7:].strip()
    elif header.startswith("Basic "):
        user, token = basic_credentials(header[6:]) or (None, None)
    elif request.headers.get("x-api-key"):
        # Anthropic-Clients: Claude Code schickt ANTHROPIC_AUTH_TOKEN als Bearer, ANTHROPIC_API_KEY als x-api-key
        token, key_via = request.headers["x-api-key"].strip(), "x-api-key"
    if token:
        # Router-own identity for UI self-call: only with the startup random AND only from localhost
        if state.INTERNAL_TOKEN and not user and (request.remote or "") in ("127.0.0.1", "::1") \
                and hmac.compare_digest(token, state.INTERNAL_TOKEN):
            return state.INTERNAL_CLIENT, "internal"
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for name, c in clients.items():
            if user and name != user:
                continue
            if c.get("token_sha256") and hmac.compare_digest(h, c["token_sha256"]):
                return name, ("basic" if user else key_via)
        return None, "bad_token"
    ip = request.remote or ""
    for name, c in clients.items():
        if ip and ip in c.get("ip", []):
            return name, "ip"
    return None, None


def error_format(path):
    """Error format by path: Anthropic (/v1/messages...), OpenAI (rest of /v1), else Ollama."""
    if path.startswith("/v1/messages"):
        return anthropic_error
    return openai_error if path.startswith("/v1/") else ollama_error


def _deny(request, status, msg, why=None):
    resp = error_format(request.path)(status, msg, why)
    if status == 401:
        resp.headers["WWW-Authenticate"] = 'Bearer realm="skirnir-router"'
    return resp


def _stats_for(name):
    return state.CLIENT_STATS["clients"].setdefault(name, {"total": 0, "last_seen": 0.0, "via": None, "recent": deque(),
                                                           "rate_limited": 0, "forbidden": 0, "last_audit": 0.0})


@web.middleware
async def client_auth_middleware(request, handler):
    """API port: identify client, check rate limit, reject unknowns in enforce mode."""
    if request.path in EXEMPT_PATHS:
        return await handler(request)
    cfg = client_auth_cfg()
    if not cfg.get("clients"):          # no block configured -> port open as before stage 2
        return await handler(request)
    now = time.time()
    name, via = resolve_client(request)
    if name is None:
        ip = request.remote or "?"
        u = state.CLIENT_STATS["unauth"].setdefault(ip, {"total": 0, "bad_token": 0, "last_seen": 0.0, "user_agent": "",
                                                          "last_path": "", "last_audit": 0.0})
        u["total"] += 1
        u["last_seen"], u["last_path"], u["user_agent"] = now, request.path, request.headers.get("User-Agent", "")[:80]
        if via == "bad_token":
            u["bad_token"] += 1
        enforce = client_auth_mode() == "enforce"
        if enforce or now - u["last_audit"] >= _UNAUTH_AUDIT_EVERY_S:
            u["last_audit"] = now
            audit("auth_denied" if enforce else "auth_missing", ip=ip, path=request.path,
                  reason=via or "no_credentials", user_agent=u["user_agent"])
        if enforce:
            return _deny(request, 401, "authentication required")
        return await handler(request)
    st = _stats_for(name)
    st["total"] += 1
    st["last_seen"], st["via"] = now, via
    rpm = (cfg["clients"].get(name) or {}).get("requests_per_minute")   # skirnir-ui is not in the configuration
    if rpm:
        rec = st["recent"]
        rec.append(now)
        while rec and rec[0] < now - 60:
            rec.popleft()
        if len(rec) > rpm:
            st["rate_limited"] += 1
            audit("rate_limited", client=name, path=request.path, limit=rpm)
            return _deny(request, 429, f"rate limit: {rpm} requests per minute for client {name}")
    refused = inference_refusal(cfg["clients"].get(name) or {}, request)
    if refused is not None:
        st["forbidden"] += 1
        audit("forbidden", client=name, path=request.path, reason=refused.kind)
        return _deny(request, refused.status, refused.message, why={"code": refused.kind, "blockers": []})
    request["client"] = name
    return await handler(request)


# A client with `inference: false` (e.g. a memory service that only reranks and is identified by its source IP) may access
# only these endpoints on the API port. Everything else is locked: inference, memory gateway, session API. Allowlist instead of
# blocklist, so a later added endpoint stays closed for it until listed here. `roles` keeps its meaning
# (empty or missing = all roles); without this field such a client would have every role.
NON_INFERENCE_ALLOWED = {("POST", "/rerank"), ("GET", "/info")}


def inference_refusal(spec, request):
    """None = allowed, else the problem (403, kind inference_disabled)."""
    if spec.get("inference", True) or (request.method, request.path) in NON_INFERENCE_ALLOWED:
        return None
    return Problem(403, "client may only use POST /rerank and GET /info (inference: false)", "inference_disabled")


def client_allows(request, role, concrete):
    """None = allowed, else the reason for 403. Unauthenticated requests (observe) have no restrictions."""
    name = request.get("client")
    if not name:
        return None
    c = (client_auth_cfg().get("clients") or {}).get(name) or {}
    if concrete:
        if c.get("models", True):
            return None
        reason = f"client {name}: concrete model names not allowed (roles only)"
    else:
        roles = c.get("roles") or ["*"]
        if "*" in roles or role["name"] in roles or role["exposed"] in roles:
            return None
        reason = f"client {name}: role {role['name']} not allowed"
    _stats_for(name)["forbidden"] += 1
    audit("forbidden", client=name, model=role["exposed"], path=request.path)
    return reason


def client_auth_view():
    """For /admin/state and /admin/client_auth: mode, all configured clients (even unseen), unknowns."""
    cfg, now = client_auth_cfg(), time.time()
    out = {"mode": client_auth_mode(), "configured_mode": cfg.get("mode", "observe"), "audit_log": cfg.get("audit_log"),
           "clients": {}, "unauthenticated": {}}
    listed = dict(cfg.get("clients") or {})
    listed.setdefault(state.INTERNAL_CLIENT, {"roles": ["*"], "models": True, "internal": True})
    for name, c in listed.items():
        s = state.CLIENT_STATS["clients"].get(name)
        out["clients"][name] = {
            "identifies_by": ["internal"] if c.get("internal") else [w for w, ok in (("token", bool(c.get("token_sha256"))), ("ip", bool(c.get("ip")))) if ok],
            "roles": c.get("roles"), "models": c.get("models"), "rerank": bool(c.get("rerank")), "inference": c.get("inference", True), "requests_per_minute": c.get("requests_per_minute"),
            "total": s["total"] if s else 0, "via": s["via"] if s else None,
            "last_seen_s": round(now - s["last_seen"], 1) if s and s["last_seen"] else None,
            "last_minute": sum(1 for t in s["recent"] if t > now - 60) if s else 0,
            "rate_limited": s["rate_limited"] if s else 0, "forbidden": s["forbidden"] if s else 0,
        }
    for ip, u in state.CLIENT_STATS["unauth"].items():
        out["unauthenticated"][ip] = {"total": u["total"], "bad_token": u["bad_token"], "last_seen_s": round(now - u["last_seen"], 1),
                                      "last_path": u["last_path"], "user_agent": u["user_agent"]}
    return out


def hash_password(password, iterations=200_000, salt=None):
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2:{iterations}:{salt.hex()}:{h.hex()}"


PASSWORD_HASH = re.compile(r"pbkdf2:([1-9][0-9]{0,8}):((?:[0-9a-fA-F]{2})+):((?:[0-9a-fA-F]{2})+)")


def verify_password(password, stored):
    """Password against 'pbkdf2:<rounds>:<salt-hex>:<hash-hex>' (hash_password); False also for an unreadable entry."""
    m = PASSWORD_HASH.fullmatch(str(stored or ""))
    if not m:
        return False
    iters, salt_hex, hash_hex = m.groups()
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8", errors="replace"), bytes.fromhex(salt_hex), int(iters))
    return hmac.compare_digest(h.hex(), hash_hex)


# Verified cache for Basic Auth (2026-09-11): PBKDF2 with 200,000 rounds costs ~200 ms CPU per call - in the event loop. The UI
# polls four endpoints every 3 s per tab, the operator's Firefox held 8 connections: the router idled at 40-50 % CPU and
# every request (also /api/chat via the tunnel) waited behind the hashes ("Router/Net 2.6 s" in the load test). A once
# verified pair (header + stored hash) is valid for BASIC_CACHE_S. Review 2026-09-25: hashing ran in the event loop and
# failures were uncached - the control port is reachable via public_url, a few wrong requests per second would have
# slowed down every inference. Now: PBKDF2 in a thread, one failure (header + hash) is rejected for BASIC_FAIL_S,
# and after BASIC_FAIL_MAX failures from a source address in BASIC_FAIL_S the router responds 429 without hashing.
BASIC_CACHE_S = 600
BASIC_FAIL_S = 60
BASIC_FAIL_MAX = 5
_BASIC_OK = {}     # sha256(header|stored) -> Ablaufzeit
_BASIC_FAIL = {}   # sha256(header|stored) -> Ablaufzeit
_BASIC_SRC = {}    # Source address -> deque(timestamps of failures)


def _prune(cache, now, limit=256):
    if len(cache) > limit:
        for k in [k for k, v in cache.items() if v <= now]:
            cache.pop(k, None)
        if len(cache) > limit:
            cache.clear()


def _basic_cached(key):
    """True/False from cache, None = must be hashed."""
    now = time.time()
    if _BASIC_OK.get(key, 0) > now:
        return True
    if _BASIC_FAIL.get(key, 0) > now:
        return False
    _prune(_BASIC_OK, now)
    _prune(_BASIC_FAIL, now)
    return None


def _basic_note(key, ok):
    (_BASIC_OK if ok else _BASIC_FAIL)[key] = time.time() + (BASIC_CACHE_S if ok else BASIC_FAIL_S)


def _basic_fail_src(remote, note):
    """Failures per source address; True = throttled (429 without hashing)."""
    now = time.time()
    q = _BASIC_SRC.setdefault(remote, deque())
    while q and q[0] <= now - BASIC_FAIL_S:
        q.popleft()
    if note:
        q.append(now)
    if len(_BASIC_SRC) > 1000:
        for k in [k for k, v in _BASIC_SRC.items() if not v or v[-1] <= now - BASIC_FAIL_S]:
            _BASIC_SRC.pop(k, None)
    return len(q) >= BASIC_FAIL_MAX


@web.middleware
async def basic_auth_middleware(request, handler):
    """UI and /admin/* require Basic Auth; the agent heartbeat (/v1/...) stays on the token."""
    if request.path.startswith("/v1/") or not state.CFG.control_users:
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    remote = request.remote or "?"
    ok = False
    if auth.startswith("Basic "):
        user, pw = basic_credentials(auth[6:]) or (None, None)
        stored = state.CFG.control_users.get(user) if user is not None else None
        if stored:
            key = hashlib.sha256((auth + "|" + stored).encode("utf-8")).hexdigest()
            ok = _basic_cached(key)
            if ok is None:
                if _basic_fail_src(remote, note=False):
                    return web.json_response({"error": "too many failed logins, try again in a minute"}, status=429,
                                             headers={"Retry-After": str(BASIC_FAIL_S)})
                ok = await asyncio.to_thread(verify_password, pw, stored)   # PBKDF2 (~200 ms) not in the event loop
                _basic_note(key, ok)
        if not ok:
            _basic_fail_src(remote, note=True)
    if not ok:
        return web.json_response({"error": "authentication required"}, status=401,
                                 headers={"WWW-Authenticate": 'Basic realm="skirnir-router", charset="UTF-8"'})
    return await handler(request)
