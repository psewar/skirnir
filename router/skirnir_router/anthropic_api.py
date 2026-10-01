"""Anthropic-Messages-API (POST /v1/messages) als Uebersetzung auf die native Ollama-API.

Claude Code und andere Anthropic-Clients sprechen /v1/messages. Der Router uebersetzt nach /api/chat und zurueck, damit
Rollen, num_ctx je Stufe (geladener Kontext nach der 0.3.7-Regel), warm zuerst, gpu_busy-Warten und die Tool-Call-Rettung
genauso gelten wie fuer Ollama- und OpenAI-Clients. Ollamas eigenes /v1/messages kennt kein num_ctx: das Modell luede mit
OLLAMA_CONTEXT_LENGTH neu und verdraengte den Runner, den alle anderen Clients teilen.

Die Uebersetzungsfunktionen sind rein (ohne Router-Zustand) und werden von test/selftest_anthropic_messages.py geprueft.
"""

import json
import secrets
import time

from aiohttp import web

from . import kontextpruefung, proxy, state
from .common import anthropic_error, anthropic_error_body, log, parse_tool_args

KEEPALIVE_S = 15            # Default von router.anthropic.keepalive_s (Prefill bei 196k Kontext bis ~2 min)
TOOL_ERROR_PREFIX = "Error: "
THINK_TYPES = ("enabled", "adaptive")
_seen_headers = set()       # anthropic-version/-beta je Wert einmal ins Log


def tool_id():
    return "toolu_" + secrets.token_hex(12)


# --- Anfrage: Anthropic -> Ollama /api/chat --------------------------------------------------------------------------

def text_of(content, sep="\n\n"):
    """String oder Liste von Bloecken -> Text der text-Bloecke (cache_control u. a. Felder fallen weg)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    return sep.join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")


def image_data(block):
    """Bild-Block -> (Base64, None) oder (None, Klartext). Ollama nimmt nur Base64; eine URL holt der Router nicht."""
    src = block.get("source") if isinstance(block.get("source"), dict) else {}
    if src.get("type") == "base64" and src.get("data"):
        return src["data"], None
    return None, f"image source type {src.get('type')!r} is not supported by the router (only base64)"


def _tool_result(block, names):
    """tool_result -> eine Ollama-Nachricht role=tool. Der Werkzeugname kommt aus dem passenden tool_use im Verlauf."""
    content = block.get("content")
    text = text_of(content)
    images = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "image":
            data, err = image_data(b)
            if err:
                return None, err
            images.append(data)
    if block.get("is_error"):
        text = TOOL_ERROR_PREFIX + text
    msg = {"role": "tool", "content": text}
    name = names.get(block.get("tool_use_id"))
    if name:
        msg["tool_name"] = name
    if images:
        msg["images"] = images
    return msg, None


def messages_to_native(messages):
    """Verlauf Anthropic -> Ollama. Liefert (Nachrichten, None) oder (None, Klartext fuer 400).

    Ein User-Zug mit tool_result-Bloecken wird zu je einer role=tool-Nachricht (in Reihenfolge), sein Text und seine Bilder
    danach zu einer user-Nachricht. thinking/redacted_thinking im Verlauf gehen nicht an das Modell zurueck.

    role=system mitten im Verlauf kennt die oeffentliche API nicht, Claude Code (2.1.x) schickt sie aber (Hinweise an das
    Modell nach dem ersten User-Zug, Textbloecke mit cache_control). Sie geht an derselben Stelle als system-Nachricht weiter."""
    out, names = [], {}
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant", "system"):
            return None, f"messages.{i}: role must be 'user', 'assistant' or 'system'"
        role, content = m["role"], m.get("content")
        if role == "system":
            text = text_of(content)
            if text:
                out.append({"role": "system", "content": text})
            continue
        if content is None or isinstance(content, str):
            out.append({"role": role, "content": content or ""})
            continue
        if not isinstance(content, list):
            return None, f"messages.{i}.content must be a string or a list of content blocks"
        texts, images, calls, tool_msgs = [], [], [], []
        for j, b in enumerate(content):
            typ = b.get("type") if isinstance(b, dict) else None
            if typ == "text":
                texts.append(b.get("text") or "")
            elif typ == "image":
                data, err = image_data(b)
                if err:
                    return None, f"messages.{i}.content.{j}: {err}"
                images.append(data)
            elif typ == "tool_use" and role == "assistant":
                names[b.get("id")] = b.get("name") or ""
                calls.append({"function": {"name": b.get("name") or "", "arguments": parse_tool_args(b.get("input"))}})
            elif typ == "tool_result" and role == "user":
                msg, err = _tool_result(b, names)
                if err:
                    return None, f"messages.{i}.content.{j}: {err}"
                tool_msgs.append(msg)
            elif typ in ("thinking", "redacted_thinking"):
                continue
            else:
                return None, f"messages.{i}.content.{j}: content block type {typ!r} is not supported by the router"
        out.extend(tool_msgs)
        if role == "assistant":
            msg = {"role": "assistant", "content": "\n\n".join(texts)}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        elif texts or images or not tool_msgs:
            msg = {"role": "user", "content": "\n\n".join(texts)}
            if images:
                msg["images"] = images
            out.append(msg)
    return out, None


def tools_to_native(tools):
    """Werkzeuge mit input_schema -> Ollama-Funktionen. Anthropic-eigene Server-Werkzeuge (web_search_..., bash_... ohne
    Schema) kann kein lokales Modell ausfuehren; sie fallen weg und stehen einmal im Log."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("type") not in (None, "custom") or "input_schema" not in t:
            _log_once(f"tool:{t.get('type')}:{t.get('name')}", "Anthropic-Werkzeug %s (Typ %s) ohne Schema ausgelassen",
                      t.get("name"), t.get("type"))
            continue
        out.append({"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                      "parameters": t.get("input_schema") or {"type": "object"}}})
    return out


def tool_choice_hint(choice, tools):
    """any/tool erzwingt Ollama nicht; best effort per Systemhinweis. auto/fehlt -> nichts."""
    if not isinstance(choice, dict) or not tools:
        return ""
    if choice.get("type") == "any":
        return "You must answer with a call to one of the available tools."
    if choice.get("type") == "tool" and choice.get("name"):
        return f"You must answer with a call to the tool `{choice['name']}`."
    return ""


def options_of(b):
    o = {}
    if b.get("max_tokens") is not None:
        o["num_predict"] = int(b["max_tokens"])
    for k in ("temperature", "top_p"):
        if b.get(k) is not None:
            o[k] = float(b[k])
    if b.get("top_k") is not None:
        o["top_k"] = int(b["top_k"])
    if b.get("stop_sequences"):
        o["stop"] = list(b["stop_sequences"])
    return o


def think_of(b):
    """thinking {type: enabled|adaptive} -> denken; fehlt/disabled -> nicht (qwen3.8 dachte sonst immer).
    adaptive (Claude Code 2.1.x) heisst bei Anthropic "das Modell entscheidet"; qwen3.8 denkt ohnehin, also true."""
    t = b.get("thinking")
    return isinstance(t, dict) and t.get("type") in THINK_TYPES


def thinking_shown(b):
    """thinking.display "omitted": der Client will den Denktext nicht (Claude Code zeigt ihn nicht an). Die API liefert dann
    thinking-Bloecke mit leerem Text; der Router ebenso."""
    t = b.get("thinking")
    return not (isinstance(t, dict) and t.get("display") == "omitted")


def to_native(b):
    """Anthropic-Body -> (Ollama-/api/chat-Body, None) oder (None, Klartext fuer 400)."""
    if not b.get("model"):
        return None, "model is required"
    if not isinstance(b.get("messages"), list) or not b["messages"]:
        return None, "messages is required"
    if b.get("max_tokens") is not None and not isinstance(b["max_tokens"], int):
        return None, "max_tokens must be an integer"
    msgs, err = messages_to_native(b["messages"])
    if err:
        return None, err
    tools = tools_to_native(b.get("tools"))
    choice = b.get("tool_choice") if isinstance(b.get("tool_choice"), dict) else {}
    if choice.get("type") == "none":
        tools = []
    system = text_of(b.get("system"))
    hint = tool_choice_hint(choice, tools)
    if hint:
        system = f"{system}\n\n{hint}" if system else hint
    if system:
        msgs = [{"role": "system", "content": system}] + msgs
    native = {"model": b["model"], "messages": msgs, "stream": bool(b.get("stream", False)), "options": options_of(b),
              "think": think_of(b)}
    if tools:
        native["tools"] = tools
    if "routing" in b:
        native["routing"] = b["routing"]   # routing-Block (nicht-standard) wie bei OpenAI; der Router nimmt ihn heraus
    return native, None


# --- Antwort: Ollama -> Anthropic ------------------------------------------------------------------------------------

def stop_reason(j, has_tools):
    """Ollama unterscheidet Stop-Sequenz und natuerliches Ende nicht (beides done_reason=stop) -> end_turn."""
    if has_tools:
        return "tool_use"
    return "max_tokens" if j.get("done_reason") == "length" else "end_turn"


def usage_of(j):
    return {"input_tokens": int(j.get("prompt_eval_count") or 0), "output_tokens": int(j.get("eval_count") or 0)}


def tool_blocks(native_calls):
    out = []
    for tc in native_calls or []:
        fn = (tc or {}).get("function") or {}
        out.append({"type": "tool_use", "id": tool_id(), "name": fn.get("name") or "",
                    "input": parse_tool_args(fn.get("arguments"))})
    return out


def sse(event, obj):
    return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


class AnthropicShape:
    """Formt Antworten von /api/chat als Anthropic-Message bzw. SSE-Ereignisfolge (message_start, Bloecke, message_delta,
    message_stop). Ein Block wird geschlossen, bevor der naechste beginnt; Indizes laufen fortlaufend."""
    name = "anthropic"
    stream_content_type = "text/event-stream"
    error = staticmethod(anthropic_error)

    def __init__(self, model, keepalive_s=KEEPALIVE_S, show_thinking=True):
        self.id = "msg_" + secrets.token_hex(12)
        self.model = model
        self.keepalive_s = keepalive_s   # proxy._connect: Ping-Abstand, solange der Knoten noch nichts geschickt hat
        self.show_thinking = show_thinking   # False bei thinking.display "omitted": Denkbloecke ohne Text
        self.last_sent = time.monotonic()    # verschwiegenes Denken: ping, damit der Stream nicht minutenlang stumm ist
        self.index = -1
        self.open = None        # Typ des offenen Blocks: thinking | text | None
        self.saw_tools = False
        self.finished = False

    def bind(self, request_id, model):
        """Vom Relay vor dem ersten Byte: Request-ID (gleich wie X-Skirnir-Request-Id) und Name, den der Client kennt."""
        self.id = f"msg_{request_id}"
        self.model = model

    # -- nicht-streamend --

    def message(self, j):
        msg = j.get("message") or {}
        content = []
        if msg.get("thinking"):
            content.append({"type": "thinking", "thinking": msg["thinking"] if self.show_thinking else "", "signature": ""})
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        tools = tool_blocks(msg.get("tool_calls"))
        content += tools
        out = {"id": self.id, "type": "message", "role": "assistant", "model": j.get("model") or self.model,
               "content": content, "stop_reason": stop_reason(j, bool(tools)), "stop_sequence": None, "usage": usage_of(j)}
        if j.get("routing"):
            out["routing"] = j["routing"]
        return out

    def complete(self, j):
        return json.dumps(self.message(j), ensure_ascii=False).encode()

    # -- Stream --

    def head(self):
        return sse("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 1}}})

    @staticmethod
    def ping():
        return sse("ping", {"type": "ping"})

    def _close(self):
        if self.open is None:
            return b""
        out = b""
        if self.open == "thinking":
            out += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                               "delta": {"type": "signature_delta", "signature": ""}})
        out += sse("content_block_stop", {"type": "content_block_stop", "index": self.index})
        self.open = None
        return out

    def _start(self, block):
        out = self._close()
        self.index += 1
        out += sse("content_block_start", {"type": "content_block_start", "index": self.index, "content_block": block})
        return out

    def _delta(self, kind, text):
        out = b""
        if self.open != kind:
            out += self._start({"type": kind, kind: ""})
            self.open = kind
        if kind == "thinking" and not self.show_thinking:
            # Denktext verschwiegen: Block bleibt (wie bei der API), Deltas nicht. Ohne sie kaeme bei langem Denken
            # minutenlang nichts beim Client an - darum hoechstens alle keepalive_s ein ping.
            if not out and time.monotonic() - self.last_sent >= self.keepalive_s:
                out = self.ping()
            return out
        field, dtype = ("thinking", "thinking_delta") if kind == "thinking" else ("text", "text_delta")
        return out + sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                 "delta": {"type": dtype, field: text}})

    def _tool(self, block):
        """Ollama liefert Tool-Calls am Stueck: Block mit leerem input, ein input_json_delta mit dem ganzen JSON, Stop."""
        args = block.pop("input")
        out = self._start({**block, "input": {}})
        out += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(args, ensure_ascii=False)}})
        out += sse("content_block_stop", {"type": "content_block_stop", "index": self.index})
        return out

    def _end(self, j):
        """Zaehler haengen am Abschluss-Chunk (wie bei toolcall_rescue) - message_delta darum erst hier."""
        self.finished = True
        out = self._close()
        out += sse("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": stop_reason(j, self.saw_tools), "stop_sequence": None},
                                     "usage": usage_of(j)})
        return out + sse("message_stop", {"type": "message_stop"})

    def chunk(self, j):
        msg = j.get("message") or {}
        out = b""
        if msg.get("thinking"):
            out += self._delta("thinking", msg["thinking"])
        if msg.get("content"):
            out += self._delta("text", msg["content"])
        for block in tool_blocks(msg.get("tool_calls")):
            self.saw_tools = True
            out += self._tool(block)
        if j.get("done") and not self.finished:
            out += self._end(j)
        if out:
            self.last_sent = time.monotonic()
        return out

    def tail(self):
        """Stream ohne Abschluss-Chunk (Knoten brach ab): trotzdem sauber beenden, damit der Client nicht haengt."""
        return b"" if self.finished else self._end({})

    def stream_error(self, status, msg, why=None):
        _status, body, _headers = anthropic_error_body(status, msg, why)
        self.finished = True
        return sse("error", body)


# --- Endpunkte -------------------------------------------------------------------------------------------------------

def _log_once(key, fmt, *args):
    if key in _seen_headers:
        return
    _seen_headers.add(key)
    log.info(fmt, *args)


def note_headers(request):
    """anthropic-version und anthropic-beta annehmen, nicht auswerten; jeden Wert einmal ins Log (Diagnose neuer Clients)."""
    for h in ("anthropic-version", "anthropic-beta"):
        v = request.headers.get(h)
        if v:
            _log_once(f"{h}:{v}", "Anthropic-Client %s: %s", h, v[:200])


async def _body(request):
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001 - aiohttp wirft je nach Fall JSONDecodeError, UnicodeDecodeError oder ClientError
        return None, anthropic_error(400, "invalid json")
    if not isinstance(b, dict):
        return None, anthropic_error(400, "body must be a json object")
    return b, None


async def handle_messages(request):
    note_headers(request)
    b, e = await _body(request)
    if e:
        return e
    native, err = to_native(b)
    if err:
        return anthropic_error(400, err)
    shape = AnthropicShape(b["model"], state.CFG.anthropic_keepalive_s, thinking_shown(b))
    return await proxy.route_request(request, "/api/chat", native, shape, anthropic_error)


async def handle_count_tokens(request):
    """Schaetzung wie die Kontextpruefung (Zeichen je Token), nie 404: Claude Code ruft den Pfad je nach Version."""
    note_headers(request)
    b, e = await _body(request)
    if e:
        return e
    native, err = to_native(b)
    if err:
        return anthropic_error(400, err)
    return web.json_response({"input_tokens": kontextpruefung.schaetze_body(native)})


def wants_anthropic(request):
    """GET /v1/models teilen sich OpenAI- und Anthropic-Clients; Anthropic-SDKs schicken immer anthropic-version."""
    return "anthropic-version" in request.headers


def model_entry(t):
    created = str(t.get("modified_at") or "2026-01-01T00:00:00Z")[:19] + "Z"
    return {"type": "model", "id": t["name"], "display_name": t["name"], "created_at": created}


def models_response(tags):
    data = [model_entry(t) for t in tags]
    return web.json_response({"data": data, "has_more": False, "first_id": data[0]["id"] if data else None,
                              "last_id": data[-1]["id"] if data else None})


def model_response(tags, mid):
    for t in tags:
        if t["name"] == mid or t["name"] == f"{mid}:latest":
            return web.json_response(model_entry(t))
    return anthropic_error(404, f"model '{mid}' not found")
