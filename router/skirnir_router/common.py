"""Konstanten, Logger und reine Helfer ohne Zustand. Werden per Name importiert."""

import base64
import json
import logging
import os
import re

from aiohttp import web


VERSION = "0.9.1"
GIB = 2 ** 30
log = logging.getLogger("router")

PROXY_METHODS = ("POST",)
PRIORITIES = ("interactive", "normal", "batch")              # Stufe 3: Prioritaetsklassen; die Reihenfolge ist die Rangfolge (admission)
DATA_CLASSES_DEFAULT = ["public", "internal", "personal", "secret"]   # Stufe 5: Datenklassen, wenn router.cloud.data_classes fehlt


def read_env_value(path, key, strip_quotes=False):
    """KEY=VALUE roh aus einer Datei (kein systemd-EnvironmentFile-Parsing: Sonderzeichen wie \\ oder " bleiben erhalten).
    None, wenn Datei oder Schluessel fehlen. strip_quotes entfernt umschliessende Anfuehrungszeichen (Cloud-Schluessel)."""
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
    """Geparste JSON-Datei; None, wenn sie fehlt oder unlesbar ist (dann mit Warnung). Der Aufrufer prueft die Form."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:  # exception-ok: file system and the file's JSON (JSONDecodeError, UnicodeDecodeError)
        log.warning("%s unreadable: %s", what, e)
        return None


def save_json_file(path, data, what, **dump_kw):
    """Atomar schreiben (tmp + replace). False mit Warnung, wenn es nicht geht; der Stand bleibt beim Aufrufer im Speicher."""
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
    """'sha256:AB:CD:...' oder 64 Hexziffern -> 64 Hexziffern klein; None, wenn es kein SHA-256-Fingerprint ist."""
    fp = str(hexfp or "").lower().removeprefix("sha256:").replace(":", "").strip()
    return fp if _TLS_FP.fullmatch(fp) else None


BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")


def basic_credentials(value):
    """(user, passwort) aus dem Wert eines Basic-Authorization-Headers (ohne 'Basic '), None wenn er kein Base64 ist.
    Ungueltiges UTF-8 wird ersetzt - so ein Passwort passt zu keinem gespeicherten, wie vorher die Ablehnung."""
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
    """'AA-BB-CC-DD-EE-01' / 'aabbccddee01' -> 'aa:bb:cc:dd:ee:01'; None, wenn es keine MAC ist."""
    s = str(mac or "").strip()
    if not _MAC_RE.fullmatch(s):
        return None
    h = re.sub(r"[:-]", "", s).lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))
INFER_PATHS = {"/api/chat", "/api/generate", "/api/embed", "/api/embeddings"}
FORBIDDEN_PATHS = {"/api/pull", "/api/push", "/api/create", "/api/copy", "/api/delete"}


def _why_headers(why):
    """Retry-After nur, wenn Warten hilft (scheduler.RETRY_AFTER_S) - OpenAI-Clients (z. B. Hermes Agent) richten ihr Backoff danach."""
    if why and why.get("retry_after_s"):
        return {"Retry-After": str(int(why["retry_after_s"]))}
    return None


def ollama_error(status, msg, why=None):
    """`why` (scheduler.unavailable) legt Grund-Code und Hindernisse maschinenlesbar neben den Klartext."""
    body = {"error": msg}
    if why:
        body.update(code=why["code"], retry_after=why.get("retry_after_s"), blockers=why["blockers"])
    return web.json_response(body, status=status, headers=_why_headers(why))


async def read_json(request):
    """Body als JSON lesen; None bei ungueltigem JSON. Die Fehlerantwort baut der Aufrufer im Format seines Clients."""
    try:
        return await request.json()
    # aiohttp wirft je nach Fall JSONDecodeError, UnicodeDecodeError oder ClientError
    except Exception:  # noqa: BLE001  # exception-ok: untrusted request body and the connection it arrives on
        return None


def safe_node_name(name):
    out = "".join(ch for ch in str(name).lower() if ch.isalnum() or ch in "-_.")
    return (out or "knoten")[:40]


# Node-RED-MCP (llm-call, ai-agent) und andere OpenAI-Clients. Der Router uebersetzt selbst nach /api/chat und zurueck,
# damit Rollen, num_ctx pro Tier, keep_alive, Tempo-Statistik und "warm zuerst" genauso gelten wie fuer Ollama-Clients.
# Ollamas eigenes /v1 wuerde das Modell ohne num_ctx mit OLLAMA_CONTEXT_LENGTH neu laden (HA-Modell weg, 15-20 s).
def openai_error(status, msg, why=None):
    """OpenAI-Form: der Grund-Code steht in error.code (wertet z. B. die Fehlerklassifikation von Hermes Agent aus), die Wartezeit in
    error.retry_after und im Header Retry-After, die Hindernisse je Knoten in error.skirnir.blockers."""
    e = {"message": msg, "type": "invalid_request_error" if status < 500 else "api_error", "param": None,
         "code": why["code"] if why else None}
    if why:
        e.update(retry_after=why.get("retry_after_s"), skirnir={"blockers": why["blockers"]})
    return web.json_response({"error": e}, status=status, headers=_why_headers(why))


# Anthropic-Messages-API (Claude Code u. a.), siehe anthropic_api.py. Kein Platz (503 im Router) heisst dort 529
# overloaded_error: Claude Code wiederholt 529 von selbst, 503 je nach Version nicht.
ANTHROPIC_OVERLOADED = 529
THINK_MODES = ("auto", "on", "off")   # router.anthropic.think: dem Client folgen / immer / nie
ANTHROPIC_ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
                         404: "not_found_error", 413: "request_too_large", 422: "invalid_request_error",
                         429: "rate_limit_error", 529: "overloaded_error"}


def anthropic_error_body(status, msg, why=None):
    """(HTTP-Status, Fehlerobjekt, Kopfzeilen) in Anthropic-Form; gemeinsam fuer die Antwort und `event: error` im Stream.
    Grund-Code, Wartezeit und Hindernisse je Knoten stehen in error.skirnir wie bei OpenAI in error.code/skirnir."""
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
