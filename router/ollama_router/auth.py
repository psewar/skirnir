"""Passwort-Hashes und Basic Auth fuer die Control-Plane; Client-Authentifizierung fuer den API-Port (Stufe 2)."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from collections import deque

from aiohttp import web

from . import state
from .common import log, ollama_error, openai_error


# ----------------------------------------------------------------------------- Client-Authentifizierung (11434)
# Wer den Router rufen darf, steht in config.yaml unter router.client_auth. Drei Wege der Identifikation, in dieser
# Reihenfolge: "Authorization: Bearer <token>" (HA-Ollama-Integration, OpenAI-Clients), "Authorization: Basic
# <client>:<token>" (Clients, die nur Basic koennen), Quell-IP (Clients, die gar keinen Header senden koennen -
# node-red-contrib-ollama schickt seinen API-Key nur an ollama.com). Tokens sind zufaellig und lang, deshalb reicht
# sha256 statt PBKDF2: die Pruefung laeuft bei jeder Anfrage, ein langsamer Hash waere hier Selbstsabotage.
# Ein falsches Token faellt NICHT auf die IP zurueck - es ist ein Fehler, keine Anonymitaet.
EXEMPT_PATHS = {"/", "/api/version"}   # Gesundheitsabfragen bleiben ohne Identitaet erreichbar (Smoke-Test, Monitoring)
_UNAUTH_AUDIT_EVERY_S = 300            # pro Quell-IP hoechstens eine Audit-Zeile in dieser Zeit (sonst flutet ein Poller das Log)
_audit_failed = [False]


def client_auth_cfg():
    return getattr(state.CFG, "client_auth", None) or {"mode": "observe", "clients": {}, "audit_log": None}


def client_auth_mode():
    return state.CLIENT_AUTH_MODE or client_auth_cfg().get("mode", "observe")


def audit(event, **fields):
    """Sicherheitsrelevantes Ereignis als JSON-Zeile ins Audit-Log (config: client_auth.audit_log) und ins Journal.
    Keine Prompts, keine Tokens - nur wer, was, warum. Scheitert das Schreiben, laeuft der Router weiter."""
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
        except Exception as e:  # noqa: BLE001
            if not _audit_failed[0]:
                log.warning("Audit-Log %s nicht schreibbar (%s) - Ereignisse nur noch im Journal", path, e)
                _audit_failed[0] = True
    log.info("AUDIT %s", line)


def resolve_client(request):
    """(Client-Name, Weg) oder (None, Grund). Weg: bearer | basic | ip; Grund: bad_token | None (nichts geschickt)."""
    clients = client_auth_cfg().get("clients") or {}
    header = request.headers.get("Authorization", "")
    token, user = None, None
    if header.startswith("Bearer "):
        token = header[7:].strip()
    elif header.startswith("Basic "):
        try:
            user, _, token = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:  # noqa: BLE001
            token = None
    if token:
        # Router-eigene Identitaet fuer den Selbstaufruf der UI: nur mit dem Start-Zufall UND nur von localhost
        if state.INTERNAL_TOKEN and not user and (request.remote or "") in ("127.0.0.1", "::1") \
                and hmac.compare_digest(token, state.INTERNAL_TOKEN):
            return state.INTERNAL_CLIENT, "internal"
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for name, c in clients.items():
            if user and name != user:
                continue
            if c.get("token_sha256") and hmac.compare_digest(h, c["token_sha256"]):
                return name, ("basic" if user else "bearer")
        return None, "bad_token"
    ip = request.remote or ""
    for name, c in clients.items():
        if ip and ip in c.get("ip", []):
            return name, "ip"
    return None, None


def _deny(request, status, msg):
    resp = (openai_error if request.path.startswith("/v1/") else ollama_error)(status, msg)
    if status == 401:
        resp.headers["WWW-Authenticate"] = 'Bearer realm="ollama-router"'
    return resp


def _stats_for(name):
    return state.CLIENT_STATS["clients"].setdefault(name, {"total": 0, "last_seen": 0.0, "via": None, "recent": deque(),
                                                           "rate_limited": 0, "forbidden": 0, "last_audit": 0.0})


@web.middleware
async def client_auth_middleware(request, handler):
    """API-Port: Client identifizieren, Rate Limit pruefen, im enforce-Modus Unbekannte abweisen."""
    if request.path in EXEMPT_PATHS:
        return await handler(request)
    cfg = client_auth_cfg()
    if not cfg.get("clients"):          # kein Block konfiguriert -> Port offen wie vor Stufe 2
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
    rpm = (cfg["clients"].get(name) or {}).get("requests_per_minute")   # skirnir-ui steht nicht in der Konfiguration
    if rpm:
        rec = st["recent"]
        rec.append(now)
        while rec and rec[0] < now - 60:
            rec.popleft()
        if len(rec) > rpm:
            st["rate_limited"] += 1
            audit("rate_limited", client=name, path=request.path, limit=rpm)
            return _deny(request, 429, f"rate limit: {rpm} requests per minute for client {name}")
    request["client"] = name
    return await handler(request)


def client_allows(request, role, concrete):
    """None = erlaubt, sonst der Grund fuer 403. Unauthentifizierte Anfragen (observe) haben keine Schranken."""
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
    """Fuer /admin/state und /admin/client_auth: Modus, alle konfigurierten Clients (auch nie gesehene), Unbekannte."""
    cfg, now = client_auth_cfg(), time.time()
    out = {"mode": client_auth_mode(), "configured_mode": cfg.get("mode", "observe"), "audit_log": cfg.get("audit_log"),
           "clients": {}, "unauthenticated": {}}
    listed = dict(cfg.get("clients") or {})
    listed.setdefault(state.INTERNAL_CLIENT, {"roles": ["*"], "models": True, "internal": True})
    for name, c in listed.items():
        s = state.CLIENT_STATS["clients"].get(name)
        out["clients"][name] = {
            "identifies_by": ["internal"] if c.get("internal") else [w for w, ok in (("token", bool(c.get("token_sha256"))), ("ip", bool(c.get("ip")))) if ok],
            "roles": c.get("roles"), "models": c.get("models"), "requests_per_minute": c.get("requests_per_minute"),
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


def verify_password(password, stored):
    try:
        _, iters, salt_hex, hash_hex = stored.split(":")
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(h.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


# Geprueft-Cache fuer Basic Auth (2026-09-11): PBKDF2 mit 200 000 Runden kostet ~200 ms CPU je Aufruf - im Event-Loop. Die UI
# fragt je Tab alle 3 s vier Endpunkte ab, Firefox des Betreibers hielt 8 Verbindungen: der Router lag im Leerlauf bei 40-50 % CPU und
# jede Anfrage (auch /api/chat ueber den Tunnel) wartete hinter den Hashes ("Router/Netz 2,6 s" beim Lasttest). Ein einmal
# geprueftes Paar (Header + gespeicherter Hash) gilt BASIC_CACHE_S. Review 2026-09-25: das Hashing lief im Event-Loop und
# Fehlversuche waren ungecacht - der Control-Port ist per public_url erreichbar, wenige falsche Anfragen pro Sekunde haetten
# jede Inferenz gebremst. Jetzt: PBKDF2 in einem Thread, ein Fehlversuch (Header + Hash) gilt BASIC_FAIL_S als abgelehnt,
# und nach BASIC_FAIL_MAX Fehlversuchen einer Quelladresse in BASIC_FAIL_S antwortet der Router 429 ohne zu hashen.
BASIC_CACHE_S = 600
BASIC_FAIL_S = 60
BASIC_FAIL_MAX = 5
_BASIC_OK = {}     # sha256(header|stored) -> Ablaufzeit
_BASIC_FAIL = {}   # sha256(header|stored) -> Ablaufzeit
_BASIC_SRC = {}    # Quelladresse -> deque(Zeitpunkte der Fehlversuche)


def _prune(cache, now, limit=256):
    if len(cache) > limit:
        for k in [k for k, v in cache.items() if v <= now]:
            cache.pop(k, None)
        if len(cache) > limit:
            cache.clear()


def _basic_cached(key):
    """True/False aus dem Cache, None = muss gehasht werden."""
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
    """Fehlversuche je Quelladresse; True = gebremst (429 ohne Hashing)."""
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
    """UI und /admin/* verlangen Basic Auth; der Agent-Heartbeat (/v1/...) bleibt beim Token."""
    if request.path.startswith("/v1/") or not state.CFG.control_users:
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    remote = request.remote or "?"
    ok = False
    if auth.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            stored = state.CFG.control_users.get(user)
            if stored:
                key = hashlib.sha256((auth + "|" + stored).encode("utf-8")).hexdigest()
                ok = _basic_cached(key)
                if ok is None:
                    if _basic_fail_src(remote, note=False):
                        return web.json_response({"error": "too many failed logins, try again in a minute"}, status=429,
                                                 headers={"Retry-After": str(BASIC_FAIL_S)})
                    ok = await asyncio.to_thread(verify_password, pw, stored)   # PBKDF2 (~200 ms) nicht im Event-Loop
                    _basic_note(key, ok)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            _basic_fail_src(remote, note=True)
    if not ok:
        return web.json_response({"error": "authentication required"}, status=401,
                                 headers={"WWW-Authenticate": 'Basic realm="ollama-router", charset="UTF-8"'})
    return await handler(request)
