"""Anthropic Messages API (POST /v1/messages) translated to the native Ollama API.

Claude Code and other Anthropic clients speak /v1/messages. The router translates to /api/chat and back, so that
roles, num_ctx per tier (loaded context after the 0.3.7 rule), warm first, gpu_busy waiting, and tool call rescue
apply exactly as they do for Ollama and OpenAI clients. Ollama's own /v1/messages does not know num_ctx: it would reload the model with
OLLAMA_CONTEXT_LENGTH and evict the runner shared by all other clients.

The translation functions are pure (no router state) and are tested by test/selftest_anthropic_messages.py.
"""

import json
import secrets
import time

from aiohttp import web

from . import context_check, proxy, state
from .common import anthropic_error, anthropic_error_body, log, parse_tool_args, read_json

KEEPALIVE_S = 15            # Default for router.anthropic.keepalive_s (Prefill for 196k context up to ~2 min)
TOOL_ERROR_PREFIX = "Error: "
THINK_TYPES = ("enabled", "adaptive")
_seen_headers = set()       # anthropic-version/-beta je Wert einmal ins Log


def tool_id():
    return "toolu_" + secrets.token_hex(12)


# --- Request: Anthropic -> Ollama /api/chat --------------------------------------------------------------------------

def text_of(content, sep="\n\n"):
    """String or list of blocks -> text of text blocks (cache_control and other fields are dropped)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    return sep.join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")


def image_data(block):
    """Image block -> (Base64, None) or (None, plaintext). Ollama only accepts Base64; the router does not fetch URLs."""
    src = block.get("source") if isinstance(block.get("source"), dict) else {}
    if src.get("type") == "base64" and src.get("data"):
        return src["data"], None
    return None, f"image source type {src.get('type')!r} is not supported by the router (only base64)"


def _tool_result(block, names, preserve_tool_ids=False):
    """tool_result -> one Ollama message with role=tool. The tool name comes from the matching tool_use in the history."""
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
    if preserve_tool_ids and block.get("tool_use_id"):
        msg["tool_call_id"] = block["tool_use_id"]
    name = names.get(block.get("tool_use_id"))
    if name:
        msg["tool_name"] = name
    if images:
        msg["images"] = images
    return msg, None


def messages_to_native(messages, preserve_tool_ids=False):
    """History Anthropic -> Ollama. Returns (messages, None) or (None, plaintext for 400).

    A user turn with tool_result blocks becomes one role=tool message each (in order), followed by its text and images
    as a user message. thinking/redacted_thinking in the history are not sent back to the model.

    role=system mid-history is not known to the public API, but Claude Code (2.1.x) sends it (hints to the
    model after the first user turn, text blocks with cache_control). It passes through as a system message at the same position."""
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
                msg, err = _tool_result(b, names, preserve_tool_ids)
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
    """Tools with input_schema -> Ollama functions. Anthropic-specific server tools (web_search_..., bash_... without
    schema) cannot be executed by local models; they are dropped and logged once."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        if t.get("type") not in (None, "custom") or "input_schema" not in t:
            _log_once(f"tool:{t.get('type')}:{t.get('name')}", "Anthropic tool %s (type %s) without schema skipped",
                      t.get("name"), t.get("type"))
            continue
        out.append({"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                      "parameters": t.get("input_schema") or {"type": "object"}}})
    return out


def tool_choice_hint(choice, tools):
    """any/tool does not force Ollama; best effort via system hint. auto/missing -> nothing."""
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
    """thinking {type: enabled|adaptive} -> think; missing/disabled -> no (qwen3.8 thought always otherwise).
    adaptive (Claude Code 2.1.x) means "the model decides" at Anthropic; qwen3.8 thinks anyway, so true."""
    t = b.get("thinking")
    return isinstance(t, dict) and t.get("type") in THINK_TYPES


def thinking_shown(b):
    """thinking.display "omitted": the client does not want the thinking text (Claude Code does not display it). The API then
    returns thinking blocks with empty text; the router does the same."""
    t = b.get("thinking")
    return not (isinstance(t, dict) and t.get("display") == "omitted")


def think_for(b, mode):
    """router.anthropic.think: auto = what the client requests (think_of), on/off = fixed."""
    if mode == "on":
        return True
    if mode == "off":
        return False
    return think_of(b)


def to_native(b, think_mode="auto"):
    """Anthropic body -> (Ollama /api/chat body, None) or (None, plaintext for 400)."""
    if not b.get("model"):
        return None, "model is required"
    if not isinstance(b.get("messages"), list) or not b["messages"]:
        return None, "messages is required"
    if b.get("max_tokens") is not None and not isinstance(b["max_tokens"], int):
        return None, "max_tokens must be an integer"
    msgs, err = messages_to_native(b["messages"], preserve_tool_ids=isinstance(b.get("routing"), dict) and bool(b["routing"].get("session")))
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
              "think": think_for(b, think_mode)}
    if tools:
        native["tools"] = tools
    if "routing" in b:
        native["routing"] = b["routing"]   # routing block (non-standard) like for OpenAI; the router removes it
    return native, None


# --- Response: Ollama -> Anthropic ------------------------------------------------------------------------------------

def stop_reason(j, has_tools):
    """Ollama does not distinguish stop sequence and natural end (both done_reason=stop) -> end_turn."""
    if has_tools:
        return "tool_use"
    return "max_tokens" if j.get("done_reason") == "length" else "end_turn"


def usage_of(j):
    return {"input_tokens": int(j.get("prompt_eval_count") or 0), "output_tokens": int(j.get("eval_count") or 0)}


def tool_blocks(native_calls):
    out = []
    for tc in native_calls or []:
        fn = (tc or {}).get("function") or {}
        out.append({"type": "tool_use", "id": tc.get("id") or tool_id(), "name": fn.get("name") or "",
                    "input": parse_tool_args(fn.get("arguments"))})
    return out


def sse(event, obj):
    return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


class AnthropicShape:
    """Formats /api/chat responses as Anthropic Message or SSE event sequence (message_start, blocks, message_delta,
    message_stop). A block is closed before the next begins; indices run continuously."""
    name = "anthropic"
    stream_content_type = "text/event-stream"
    error = staticmethod(anthropic_error)

    def __init__(self, model, keepalive_s=KEEPALIVE_S, show_thinking=True):
        self.id = "msg_" + secrets.token_hex(12)
        self.model = model
        self.keepalive_s = keepalive_s   # proxy._connect: Ping interval while the node has not sent anything yet
        self.show_thinking = show_thinking   # False for thinking.display "omitted": thinking blocks without text
        self.last_sent = time.monotonic()    # redacted thinking: ping so the stream is not silent for minutes
        self.index = -1
        self.open = None        # Type of the open block: thinking | text | None
        self.saw_tools = False
        self.finished = False

    def bind(self, request_id, model):
        """From the relay before the first byte: Request-ID (same as X-Skirnir-Request-Id) and the name known to the client."""
        self.id = f"msg_{request_id}"
        self.model = model

    # -- non-streaming --

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
            # Thinking text redacted: block remains (as with the API), deltas do not. Without them, long thinking would result in
            # nothing reaching the client for minutes - hence a ping at most every keepalive_s.
            if not out and time.monotonic() - self.last_sent >= self.keepalive_s:
                out = self.ping()
            return out
        field, dtype = ("thinking", "thinking_delta") if kind == "thinking" else ("text", "text_delta")
        return out + sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                 "delta": {"type": dtype, field: text}})

    def _tool(self, block):
        """Ollama delivers tool calls in one go: block with empty input, one input_json_delta with the full JSON, Stop."""
        args = block.pop("input")
        out = self._start({**block, "input": {}})
        out += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(args, ensure_ascii=False)}})
        out += sse("content_block_stop", {"type": "content_block_stop", "index": self.index})
        return out

    def _end(self, j):
        """Counters hang on the final chunk (like in toolcall_rescue) - message_delta therefore only here."""
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
        """Stream without final chunk (node crashed): still end cleanly so the client does not hang."""
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
    """Accept anthropic-version and anthropic-beta, do not evaluate; log each value once (diagnose new clients)."""
    for h in ("anthropic-version", "anthropic-beta"):
        v = request.headers.get(h)
        if v:
            _log_once(f"{h}:{v}", "Anthropic-Client %s: %s", h, v[:200])


async def _body(request):
    b = await read_json(request)
    if b is None:
        return None, anthropic_error(400, "invalid json")
    if not isinstance(b, dict):
        return None, anthropic_error(400, "body must be a json object")
    return b, None


async def handle_messages(request):
    note_headers(request)
    b, e = await _body(request)
    if e:
        return e
    native, err = to_native(b, state.CFG.anthropic_think)
    if err:
        return anthropic_error(400, err)
    shape = AnthropicShape(b["model"], state.CFG.anthropic_keepalive_s, thinking_shown(b))
    return await proxy.route_request(request, "/api/chat", native, shape, anthropic_error)


async def handle_count_tokens(request):
    """Estimate like the context check (chars per token), never 404: Claude Code calls the path depending on version."""
    note_headers(request)
    b, e = await _body(request)
    if e:
        return e
    native, err = to_native(b)
    if err:
        return anthropic_error(400, err)
    return web.json_response({"input_tokens": context_check.estimate_body(native)})


def wants_anthropic(request):
    """GET /v1/models is shared by OpenAI and Anthropic clients; Anthropic SDKs always send anthropic-version."""
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
