"""Konstanten, Logger und reine Helfer ohne Zustand. Werden per Name importiert."""

import json
import logging
import re

from aiohttp import web


VERSION = "0.3.0"
GIB = 2 ** 30
log = logging.getLogger("router")

PROXY_METHODS = ("POST",)
PRIORITIES = ("interactive", "normal", "batch")              # Stufe 3: Prioritaetsklassen; die Reihenfolge ist die Rangfolge (admission)
DATA_CLASSES_DEFAULT = ["public", "internal", "personal", "secret"]   # Stufe 5: Datenklassen, wenn router.cloud.data_classes fehlt


def read_env_value(path, key, strip_quotes=False):
    """KEY=VALUE roh aus einer Datei (kein systemd-EnvironmentFile-Parsing: Sonderzeichen wie \\ oder " bleiben erhalten).
    None, wenn Datei oder Schluessel fehlen. strip_quotes entfernt umschliessende Anfuehrungszeichen (Cloud-Schluessel)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    v = line.split("=", 1)[1].rstrip("\r\n")
                    return v.strip().strip('"').strip("'") if strip_quotes else v
    except FileNotFoundError:
        return None
    return None


def parse_tool_args(args):
    """Tool-Argumente eines Modells: JSON-Text -> Objekt, leer -> {}, unparsbar -> {"_raw": text}; Objekte gehen 1:1 durch."""
    if not isinstance(args, str):
        return {} if args is None else args
    if not args.strip():
        return {}
    try:
        return json.loads(args)
    except ValueError:
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


def ollama_error(status, msg):
    return web.json_response({"error": msg}, status=status)


async def read_json(request):
    """Body als JSON lesen; None bei ungueltigem JSON. Die Fehlerantwort baut der Aufrufer im Format seines Clients."""
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 - aiohttp wirft je nach Fall JSONDecodeError, UnicodeDecodeError oder ClientError
        return None


def safe_node_name(name):
    out = "".join(ch for ch in str(name).lower() if ch.isalnum() or ch in "-_.")
    return (out or "knoten")[:40]


# Node-RED-MCP (llm-call, ai-agent) und andere OpenAI-Clients. Der Router uebersetzt selbst nach /api/chat und zurueck,
# damit Rollen, num_ctx pro Tier, keep_alive, Tempo-Statistik und "warm zuerst" genauso gelten wie fuer Ollama-Clients.
# Ollamas eigenes /v1 wuerde das Modell ohne num_ctx mit OLLAMA_CONTEXT_LENGTH neu laden (HA-Modell weg, 15-20 s).
def openai_error(status, msg):
    return web.json_response({"error": {"message": msg, "type": "invalid_request_error" if status < 500 else "api_error",
                                        "param": None, "code": None}}, status=status)


def split_listen(s):
    host, _, port = s.rpartition(":")
    return host or "0.0.0.0", int(port)
