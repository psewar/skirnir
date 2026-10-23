"""Constants, logger, and pure stateless helpers. Imported by name."""

import base64
import json
import logging
import os
import re

from aiohttp import web


VERSION = "0.16.5"
GIB = 2 ** 30
log = logging.getLogger("router")

PROXY_METHODS = ("POST",)
PRIORITIES = ("interactive", "normal", "batch")              # Stage 3: Priority classes; order defines rank (admission)
DATA_CLASSES_DEFAULT = ["public", "internal", "personal", "secret"]   # Stage 5: Data classes, if router.cloud.data_classes is missing


def read_env_value(path, key, strip_quotes=False):
    """KEY=VALUE raw from a file (no systemd-EnvironmentFile parsing: special chars like \\ or " are preserved).
    None if file or key is missing. strip_quotes removes surrounding quotes (Cloud keys)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:  # exception-ok: file system boundary (rights, file vanished after the check)
        log.warning("%s unreadable: %s", path, e)
        return None
    v = next((line.split("=", 1)[1] for line in lines if line.startswith(key + "=")), None)
    return v.strip().strip('"').strip("'") if v is not None and strip_quotes else v


def load_json_file(path, what):
    """Parsed JSON file; None if missing or unreadable (with warning). Caller validates shape."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:  # exception-ok: file system and the file's JSON (JSONDecodeError, UnicodeDecodeError)
        log.warning("%s unreadable: %s", what, e)
        return None


def save_json_file(path, data, what, **dump_kw):
    """Atomic write (tmp + replace). False with warning on failure; state remains with caller in memory."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kw)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:  # exception-ok: file system boundary; json reports unencodable data only so
        log.warning("%s write: %s", what, e)
        return False
    return True


_TLS_FP = re.compile(r"[0-9a-f]{64}")


def tls_fingerprint(hexfp):
    """'sha256:AB:CD:...' or 64 hex digits -> 64 lowercase hex digits; None if not a SHA-256 fingerprint."""
    fp = str(hexfp or "").lower().removeprefix("sha256:").replace(":", "").strip()
    return fp if _TLS_FP.fullmatch(fp) else None


BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")


def basic_credentials(value):
    """(user, password) from Basic-Authorization header value (without 'Basic '), None if not Base64.
    Invalid UTF-8 is replaced - such a password matches no stored one, as rejection did before."""
    value = value.strip()
    if not BASE64.fullmatch(value):
        return None
    user, _, password = base64.b64decode(value).decode("utf-8", errors="replace").partition(":")
    return user, password


def parse_tool_args(args):
    """Tool-Argumente eines Modells: JSON-Text -> Objekt, leer -> {}, unparsbar -> {"_raw": text}; Objekte gehen 1:1 durch."""
    if not isinstance(args, str):
        return {} if args is None else args
    if not args.strip():
        return {}
    try:
        return json.loads(args)
    except ValueError:  # exception-ok: the model's tool arguments are untrusted JSON; whether they parse shows only the parser
        return {"_raw": args}

_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}([:-]?)(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}")


def normalize_mac(mac):
    """'AA-BB-CC-DD-EE-01' / 'aabbccddee01' -> 'aa:bb:cc:dd:ee:01'; None if not a MAC."""
    s = str(mac or "").strip()
    if not _MAC_RE.fullmatch(s):
        return None
    h = re.sub(r"[:-]", "", s).lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))
INFER_PATHS = {"/api/chat", "/api/generate", "/api/embed", "/api/embeddings"}
FORBIDDEN_PATHS = {"/api/pull", "/api/push", "/api/create", "/api/copy", "/api/delete"}


def _why_headers(why):
    """Retry-After only if waiting helps (scheduler.RETRY_AFTER_S) - OpenAI clients (e.g. Hermes Agent) set backoff based on it."""
    if why and why.get("retry_after_s"):
        return {"Retry-After": str(int(why["retry_after_s"]))}
    return None


def ollama_error(status, msg, why=None):
    """`why` (scheduler.unavailable) places base code and blockers machine-readable next to plain text."""
    body = {"error": msg}
    if why:
        body.update(code=why["code"], retry_after=why.get("retry_after_s"), blockers=why["blockers"])
    return web.json_response(body, status=status, headers=_why_headers(why))


async def read_json(request):
    """Read body as JSON; None for invalid JSON. Caller builds error response in client format."""
    try:
        return await request.json()
    # aiohttp raises JSONDecodeError, UnicodeDecodeError, or ClientError depending on case
    except Exception:  # noqa: BLE001  # exception-ok: untrusted request body and the connection it arrives on
        return None


def safe_node_name(name):
    out = "".join(ch for ch in str(name).lower() if ch.isalnum() or ch in "-_.")
    return (out or "knoten")[:40]


# Node-RED-MCP (llm-call, ai-agent) and other OpenAI clients. Router translates to /api/chat and back,
# so roles, num_ctx per tier, keep_alive, rate stats, and "warm first" apply as with Ollama clients.
# Ollama's own /v1 would reload model without num_ctx using OLLAMA_CONTEXT_LENGTH (HA model lost, 15-20 s).
def openai_error(status, msg, why=None):
    """OpenAI format: base code in error.code (evaluates e.g. Hermes Agent error classification), wait time in
    error.retry_after and Retry-After header, blockers per node in error.skirnir.blockers."""
    e = {"message": msg, "type": "invalid_request_error" if status < 500 else "api_error", "param": None,
         "code": why["code"] if why else None}
    if why:
        e.update(retry_after=why.get("retry_after_s"), skirnir={"blockers": why["blockers"]})
    return web.json_response({"error": e}, status=status, headers=_why_headers(why))


# Anthropic-Messages-API (Claude Code u. a.), siehe anthropic_api.py. Kein Platz (503 im Router) heisst dort 529
# overloaded_error: Claude Code retries 529 automatically, 503 not depending on version.
ANTHROPIC_OVERLOADED = 529
THINK_MODES = ("auto", "on", "off")   # router.anthropic.think: follow client / always / never
ANTHROPIC_ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
                         404: "not_found_error", 413: "request_too_large", 422: "invalid_request_error",
                         429: "rate_limit_error", 529: "overloaded_error"}


def anthropic_error_body(status, msg, why=None):
    """(HTTP status, error object, headers) in Anthropic format; shared for response and `event: error` in stream.
    Base code, wait time, and blockers per node in error.skirnir like OpenAI error.code/skirnir."""
    if status == 503:
        status = ANTHROPIC_OVERLOADED
    typ = ANTHROPIC_ERROR_TYPES.get(status) or ("api_error" if status >= 500 else "invalid_request_error")
    e = {"type": typ, "message": msg}
    if why:
        e["skirnir"] = {"code": why["code"], "retry_after": why.get("retry_after_s"), "blockers": why["blockers"]}
    return status, {"type": "error", "error": e}, _why_headers(why)


def anthropic_error(status, msg, why=None):
    status, body, headers = anthropic_error_body(status, msg, why)
    return web.json_response(body, status=status, headers=headers)


def split_listen(s):
    host, _, port = s.rpartition(":")
    return host or "0.0.0.0", int(port)
