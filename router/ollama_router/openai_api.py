"""OpenAI-kompatibles /v1 als Uebersetzung auf die native Ollama-API."""

import json
import secrets
import time

from aiohttp import web

from . import proxy, state
from .common import openai_error


def oa_id(prefix):
    return f"{prefix}-{secrets.token_hex(12)}"


def oa_tool_calls(native):
    """Ollama message.tool_calls (arguments als Objekt) -> OpenAI (arguments als JSON-String, id, type)."""
    out = []
    for i, tc in enumerate(native or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments", {})
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({"id": tc.get("id") or oa_id("call"), "index": tc.get("index", i), "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args}})
    return out


def oa_finish(j, has_tools):
    if has_tools:
        return "tool_calls"
    return "length" if j.get("done_reason") == "length" else "stop"


def oa_usage(j):
    p = int(j.get("prompt_eval_count") or 0)
    c = int(j.get("eval_count") or 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def oa_messages_to_native(msgs):
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        n = {"role": m.get("role", "user")}
        content = m.get("content")
        if isinstance(content, list):   # Inhaltsteile: Text zusammenfuegen, data-URL-Bilder als Base64 uebergeben
            texts, images = [], []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "") if isinstance(part.get("image_url"), dict) else str(part.get("image_url", ""))
                    if url.startswith("data:") and "," in url:
                        images.append(url.split(",", 1)[1])
            n["content"] = "".join(texts)
            if images:
                n["images"] = images
        else:
            n["content"] = "" if content is None else str(content)
        if m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                fn = (tc or {}).get("function") or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except ValueError:
                        args = {"_raw": args}
                tcs.append({"function": {"name": fn.get("name", ""), "arguments": args}})
            n["tool_calls"] = tcs
        if m.get("role") == "tool" and m.get("name"):
            n["tool_name"] = m["name"]
        out.append(n)
    return out


def oa_options(b):
    """Sampling-Parameter OpenAI -> Ollama options."""
    o = {}
    if b.get("temperature") is not None:
        o["temperature"] = float(b["temperature"])
    if b.get("top_p") is not None:
        o["top_p"] = float(b["top_p"])
    if b.get("seed") is not None:
        o["seed"] = int(b["seed"])
    mt = b.get("max_completion_tokens", b.get("max_tokens"))
    if mt is not None:
        o["num_predict"] = int(mt)
    if b.get("stop"):
        o["stop"] = [b["stop"]] if isinstance(b["stop"], str) else list(b["stop"])
    if b.get("frequency_penalty") is not None:
        o["frequency_penalty"] = float(b["frequency_penalty"])
    if b.get("presence_penalty") is not None:
        o["presence_penalty"] = float(b["presence_penalty"])
    return o


def oa_think(b):
    """think: explizit (nicht-standard), sonst reasoning_effort ('none' = aus), sonst Konfig-Default."""
    if "think" in b:
        return bool(b["think"])
    eff = b.get("reasoning_effort")
    if eff is not None:
        return False if str(eff).lower() == "none" else True
    return state.CFG.openai_default_think


def oa_format(b):
    rf = b.get("response_format")
    if not isinstance(rf, dict):
        return None
    if rf.get("type") == "json_object":
        return "json"
    if rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        return schema or "json"
    return None


def oa_chat_to_native(b):
    native = {"model": b.get("model"), "messages": oa_messages_to_native(b.get("messages")),
              "stream": bool(b.get("stream", False)), "options": oa_options(b)}
    if b.get("tools"):
        native["tools"] = b["tools"]
    fmt = oa_format(b)
    if fmt is not None:
        native["format"] = fmt
    think = oa_think(b)
    if think is not None:
        native["think"] = think
    if "routing" in b:
        native["routing"] = b["routing"]   # Stufe 1: routing-Block (nicht-standard), Router nimmt ihn wieder heraus
    return native


class OpenAIChatShape:
    """Formt Antworten von /api/chat als chat.completion bzw. SSE-Chunks."""
    name = "openai"
    stream_content_type = "text/event-stream"

    def __init__(self, include_usage=False):
        self.id = oa_id("chatcmpl")
        self.created = int(time.time())
        self.include_usage = include_usage
        self.saw_tools = False

    error = staticmethod(openai_error)

    def complete(self, j):
        msg = j.get("message") or {}
        tcs = oa_tool_calls(msg.get("tool_calls"))
        m = {"role": "assistant", "content": msg.get("content", "") or ("" if tcs else "")}
        if tcs:
            m["tool_calls"] = tcs
            m["content"] = msg.get("content") or None
        if msg.get("thinking"):
            m["reasoning"] = msg["thinking"]
        out = {"id": self.id, "object": "chat.completion", "created": self.created, "model": j.get("model"),
               "system_fingerprint": "fp_ollama_router",
               "choices": [{"index": 0, "message": m, "finish_reason": oa_finish(j, bool(tcs))}],
               "usage": oa_usage(j)}
        if j.get("routing"):
            out["routing"] = j["routing"]
        return json.dumps(out, ensure_ascii=False).encode()

    def _sse(self, obj):
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()

    def _chunk(self, model, delta, finish=None):
        return {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": model,
                "system_fingerprint": "fp_ollama_router", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def chunk(self, j):
        msg = j.get("message") or {}
        out = b""
        delta = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("thinking"):
            delta["reasoning"] = msg["thinking"]
        tcs = oa_tool_calls(msg.get("tool_calls"))
        if tcs:
            self.saw_tools = True
            delta["tool_calls"] = tcs
        if j.get("done"):
            if len(delta) > 1:
                out += self._sse(self._chunk(j.get("model"), delta))
            fin = self._chunk(j.get("model"), {}, oa_finish(j, self.saw_tools))
            if j.get("routing"):
                fin["routing"] = j["routing"]
            out += self._sse(fin)
            if self.include_usage:
                u = self._chunk(j.get("model"), {})
                u["choices"] = []
                u["usage"] = oa_usage(j)
                out += self._sse(u)
            return out
        if len(delta) > 1:
            out += self._sse(self._chunk(j.get("model"), delta))
        return out

    def tail(self):
        return b"data: [DONE]\n\n"


class OpenAICompletionShape(OpenAIChatShape):
    """Legacy /v1/completions ueber /api/generate: text_completion."""

    def __init__(self, include_usage=False):
        super().__init__(include_usage)
        self.id = oa_id("cmpl")

    def complete(self, j):
        out = {"id": self.id, "object": "text_completion", "created": self.created, "model": j.get("model"),
               "system_fingerprint": "fp_ollama_router",
               "choices": [{"index": 0, "text": j.get("response", ""), "logprobs": None, "finish_reason": oa_finish(j, False)}],
               "usage": oa_usage(j)}
        if j.get("routing"):
            out["routing"] = j["routing"]
        return json.dumps(out, ensure_ascii=False).encode()

    def chunk(self, j):
        base = {"id": self.id, "object": "text_completion", "created": self.created, "model": j.get("model"), "system_fingerprint": "fp_ollama_router"}
        out = b""
        if j.get("response"):
            out += self._sse({**base, "choices": [{"index": 0, "text": j["response"], "logprobs": None, "finish_reason": None}]})
        if j.get("done"):
            out += self._sse({**base, "choices": [{"index": 0, "text": "", "logprobs": None, "finish_reason": oa_finish(j, False)}]})
            if self.include_usage:
                out += self._sse({**base, "choices": [], "usage": oa_usage(j)})
        return out


class OpenAIEmbedShape:
    name = "openai"
    stream_content_type = "application/json"
    error = staticmethod(openai_error)

    def complete(self, j):
        embs = j.get("embeddings") or ([j["embedding"]] if j.get("embedding") else [])
        p = int(j.get("prompt_eval_count") or 0)
        out = {"object": "list", "model": j.get("model"),
               "data": [{"object": "embedding", "index": i, "embedding": e} for i, e in enumerate(embs)],
               "usage": {"prompt_tokens": p, "total_tokens": p}}
        if j.get("routing"):
            out["routing"] = j["routing"]
        return json.dumps(out).encode()

    def chunk(self, j):
        return b""

    def tail(self):
        return b""


def oa_model_entry(t):
    created = 0
    try:
        created = int(time.mktime(time.strptime(str(t.get("modified_at", ""))[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        pass
    return {"id": t["name"], "object": "model", "created": created, "owned_by": "library"}


async def handle_oa_models(request):
    return web.json_response({"object": "list", "data": [oa_model_entry(t) for t in proxy.tags_list()]})


async def handle_oa_model(request):
    mid = request.match_info["model"]
    for t in proxy.tags_list():
        if t["name"] == mid or t["name"] == f"{mid}:latest":
            return web.json_response(oa_model_entry(t))
    return openai_error(404, f"model '{mid}' not found")


async def _oa_body(request):
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        return None, openai_error(400, "invalid json")
    if not isinstance(b, dict):
        return None, openai_error(400, "body must be a json object")
    return b, None


def _include_usage(b):
    so = b.get("stream_options")
    return bool(isinstance(so, dict) and so.get("include_usage"))


async def handle_oa_chat(request):
    b, e = await _oa_body(request)
    if e:
        return e
    if not isinstance(b.get("messages"), list) or not b["messages"]:
        return openai_error(400, "messages is required")
    native = oa_chat_to_native(b)
    return await proxy.route_request(request, "/api/chat", native, OpenAIChatShape(_include_usage(b)), openai_error)


async def handle_oa_completions(request):
    b, e = await _oa_body(request)
    if e:
        return e
    prompt = b.get("prompt", "")
    if isinstance(prompt, list):
        prompt = "".join(str(p) for p in prompt)
    native = {"model": b.get("model"), "prompt": str(prompt), "stream": bool(b.get("stream", False)), "options": oa_options(b)}
    if "routing" in b:
        native["routing"] = b["routing"]
    if b.get("suffix"):
        native["suffix"] = b["suffix"]
    think = oa_think(b)
    if think is not None:
        native["think"] = think
    return await proxy.route_request(request, "/api/generate", native, OpenAICompletionShape(_include_usage(b)), openai_error)


async def handle_oa_embeddings(request):
    b, e = await _oa_body(request)
    if e:
        return e
    if b.get("input") is None:
        return openai_error(400, "input is required")
    native = {"model": b.get("model"), "input": b["input"], "stream": False}
    if "routing" in b:
        native["routing"] = b["routing"]
    return await proxy.route_request(request, "/api/embed", native, OpenAIEmbedShape(), openai_error)


async def handle_oa_unknown(request):
    return openai_error(404, f"{request.method} {request.path} is not supported by the router")
