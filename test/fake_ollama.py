#!/usr/bin/env python3
"""Fake-Ollama for router tests. One process = one node.

python fake_ollama.py <port> <name> <modelA,modelB,...> [--delay 0.2] [--loaded model]
Serves /api/tags, /api/ps, /api/show, /api/chat (stream+non-stream), /api/generate (incl. keep_alive:0 = unload).
Tracks loaded models with num_ctx; logs every inference request to stdout as a JSON line.
"""
import asyncio
import json
import re
import sys

from aiohttp import web

port = int(sys.argv[1]); name = sys.argv[2]; models = sys.argv[3].split(",")
delay = 0.2; loaded = {}
args = sys.argv[4:]
if "--delay" in args: delay = float(args[args.index("--delay") + 1])
if "--loaded" in args: loaded[args[args.index("--loaded") + 1]] = 8192
TLS = ("--tls" in args) and (args[args.index("--tls") + 1], args[args.index("--tls") + 2])   # cert key
TOKEN = args[args.index("--require-token") + 1] if "--require-token" in args else None
SIZES = {"qwen3.6:35b-a3b": 20.6, "qwen3-coder:30b": 17.3, "granite4.2:8b": 4.95, "gpt-oss:20b": 11.95, "local-assist:latest": 20.6}
ALIAS = {"local-assist:latest": "qwen3.6:35b-a3b"}   # same digest as the original
KV = {"granite4.2:8b": 0.158, "qwen3-coder:30b": 0.096}   # GiB per 1k token context (for /api/ps size_vram)
# Capabilities as reported by /api/show (tier 1: the router filters tiers after this); coder without thinking, gpt-oss without tools
CAPS = {"qwen3.6:35b-a3b": ["completion", "tools", "thinking"], "local-assist:latest": ["completion", "tools", "thinking"],
        "qwen3-coder:30b": ["completion", "tools", "insert"], "granite4.2:8b": ["completion", "tools", "thinking"],
        "gpt-oss:20b": ["completion", "thinking"]}


HOOK_RE = re.compile(r"\[\[(\w+)(?:=([\w.]+))?\]\]")


def test_hooks(b):
    """Test helpers for paths that do not pass through their own body fields (Anthropic translation rebuilds the body):
    [[name=value]] in the last message acts like the same-named body field (sleep_s, fail_status, broken_tool_call,
    stream_tool, stream_error)."""
    msgs = b.get("messages") or []
    last = msgs[-1].get("content") if msgs and isinstance(msgs[-1], dict) else ""
    for k, v in HOOK_RE.findall(str(last or "")):
        b.setdefault(k, v or True)


def digest(m): return "d" + ALIAS.get(m, m)


def gib(m): return int(SIZES.get(m, 5.0) * 2 ** 30)


async def tags(_):
    return web.json_response({"models": [{"name": m, "model": m, "size": gib(m), "digest": digest(m),
                                          "modified_at": "2026-09-01T00:00:00Z",
                                          "details": {"family": "qwen3", "parameter_size": "x", "quantization_level": "Q4"}}
                                         for m in models]})


async def ps(_):
    def sz(m, c): return int((SIZES.get(m, 5.0) + KV.get(m, 0.0) * (c or 4096) / 1000) * 2 ** 30)
    return web.json_response({"models": [{"name": m, "model": m, "digest": digest(m), "size": sz(m, c), "size_vram": sz(m, c), "context_length": c}
                                         for m, c in loaded.items()]})


async def show(req):
    b = await req.json()
    if b.get("model") not in models:
        return web.json_response({"error": "model not found"}, status=404)
    caps = CAPS.get(b["model"], ["completion", "tools"])
    return web.json_response({"capabilities": caps, "details": {"family": "qwen3"}, "model_info": {}})


async def infer(req):
    b = await req.json()
    test_hooks(b)
    m = b.get("model")
    if m not in models:
        return web.json_response({"error": f"model '{m}' not found"}, status=404)
    if b.get("keep_alive") == 0 and not b.get("messages") and not b.get("prompt"):
        loaded.pop(m, None)
        print(json.dumps({"node": name, "unload": m}), flush=True)
        return web.json_response({"model": m, "done": True})
    if b.get("sleep_s"):       # Test helper (tier 3): long request, keeps the slot on the node busy
        await asyncio.sleep(float(b["sleep_s"]))
    if b.get("fail_status"):   # Test helper (tier 3): backend error before the first byte, e.g. 500 (with sleep_s: late)
        return web.json_response({"error": "fake failure"}, status=int(b["fail_status"]))
    if b.get("slow_load"):   # Test helper: only appears in /api/ps after this time - like a real cold start,
        # where nvidia-smi already sees the VRAM. Like real Ollama, the old model is evicted BEFORE loading:
        # /api/ps shows neither during that time (the case that led to 503 'no node' on 2026-09-25).
        while loaded and m not in loaded and sum(SIZES.get(k, 5.0) for k in loaded) + SIZES.get(m, 5.0) > 31.8:
            loaded.pop(next(iter(loaded)))
        await asyncio.sleep(float(b["slow_load"]))
    ctx = (b.get("options") or {}).get("num_ctx") or 4096   # Ollama-Default im Fake
    loaded[m] = ctx
    while len(loaded) > 1 and sum(SIZES.get(k, 5.0) for k in loaded) > 31.8:   # VRAM eviction like real Ollama
        loaded.pop(next(iter(loaded)))
    print(json.dumps({"node": name, "path": req.path, "model": m, "num_ctx": ctx, "keep_alive": b.get("keep_alive"),
                      "stream": b.get("stream", True), "tools": bool(b.get("tools")), "format": b.get("format"),
                      "think": b.get("think"), "options": b.get("options")}), flush=True)
    if req.path == "/api/generate" and b.get("prompt") is not None and not b.get("stream", True):
        await asyncio.sleep(delay)
        return web.json_response({"model": m, "response": f"Hallo von {name}", "done": True, "done_reason": "stop",
                                  "eval_count": 30, "eval_duration": int(0.5e9), "prompt_eval_count": 20, "prompt_eval_duration": int(0.02e9),
                                  "load_duration": int(5e6), "total_duration": int(0.6e9)})
    # Model writes the call in the wrong dialect: Ollama does not parse it and returns it
    # as text. This is how qwen3-coder:30b came back above ~12k tokens on 2026-09-15.
    if b.get("broken_tool_call"):
        kaputt = "\n".join(["<function=get_time>", "<parameter=tz>", "CET",
                            "</parameter>", "</function>", "</tool_call>"])
        zaehler = {"eval_count": 12, "eval_duration": int(0.2e9), "prompt_eval_count": 40,
                   "prompt_eval_duration": int(0.02e9), "load_duration": int(5e6)}
        if not b.get("stream", True):
            await asyncio.sleep(delay)
            return web.json_response({"model": m, "message": {"role": "assistant", "content": kaputt},
                                      "done": True, "done_reason": "stop", **zaehler})
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(req)
        for tok in [kaputt[i:i + 12] for i in range(0, len(kaputt), 12)]:   # chunked, like real
            await asyncio.sleep(delay)
            await resp.write((json.dumps({"model": m, "message": {"role": "assistant", "content": tok},
                                          "done": False}) + "\n").encode())
        await resp.write((json.dumps({"model": m, "message": {"role": "assistant", "content": ""},
                                      "done": True, "done_reason": "stop", **zaehler}) + "\n").encode())
        await resp.write_eof()
        return resp

    if b.get("stream_error") and b.get("stream", True):   # Abort mid-stream, wording like Ollama 0.35 on 2026-10-02
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(req)
        for tok in ["Ich ", "rufe "]:
            await asyncio.sleep(delay)
            await resp.write((json.dumps({"model": m, "message": {"role": "assistant", "content": tok}, "done": False}) + "\n").encode())
        await resp.write((json.dumps({"error": "XML syntax error on line 3: unexpected EOF"}) + "\n").encode())
        await resp.write_eof()
        return resp
    if b.get("tools") and b.get("stream_tool") and b.get("stream", True):   # Text, then the call in one chunk (like Ollama)
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(req)
        for msg in ({"role": "assistant", "content": "Ich schaue nach."},
                    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_time", "arguments": {"tz": "CET"}}}]}):
            await asyncio.sleep(delay)
            await resp.write((json.dumps({"model": m, "message": msg, "done": False}) + "\n").encode())
        await resp.write((json.dumps({"model": m, "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                                      "eval_count": 12, "eval_duration": int(0.2e9), "prompt_eval_count": 40,
                                      "prompt_eval_duration": int(0.02e9), "load_duration": int(5e6)}) + "\n").encode())
        await resp.write_eof()
        return resp
    if b.get("tools") and not b.get("stream", True):   # Tool call like native Ollama: arguments as object
        await asyncio.sleep(delay)
        return web.json_response({"model": m, "message": {"role": "assistant", "content": "",
                                                          "tool_calls": [{"function": {"name": "get_time", "arguments": {"tz": "CET"}}}]},
                                  "done": True, "done_reason": "stop", "eval_count": 12, "eval_duration": int(0.2e9),
                                  "prompt_eval_count": 40, "prompt_eval_duration": int(0.02e9), "load_duration": int(5e6)})
    if b.get("stream", True):
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(req)
        for tok in ["Hal", "lo ", f"von {name}"]:
            await asyncio.sleep(delay)
            await resp.write((json.dumps({"model": m, "message": {"role": "assistant", "content": tok}, "done": False}) + "\n").encode())
        await resp.write((json.dumps({"model": m, "message": {"role": "assistant", "content": ""}, "done": True,
                                      "eval_count": 30, "eval_duration": int(0.5e9), "prompt_eval_count": 20, "prompt_eval_duration": int(0.02e9),
                                      "load_duration": int(5e6), "total_duration": int(0.6e9)}) + "\n").encode())
        await resp.write_eof()
        return resp
    await asyncio.sleep(delay)
    return web.json_response({"model": m, "message": {"role": "assistant", "content": f"Hallo von {name}"}, "done": True,
                              "eval_count": 30, "eval_duration": int(0.5e9), "prompt_eval_count": 20, "prompt_eval_duration": int(0.02e9),
                              "load_duration": int(5e6), "total_duration": int(0.6e9)})


async def embed(req):
    b = await req.json()
    m = b.get("model")
    if m not in models:
        return web.json_response({"error": f"model '{m}' not found"}, status=404)
    inp = b.get("input")
    inputs = inp if isinstance(inp, list) else [inp]
    if any("kein-embedding" in str(x) for x in inputs):   # like Ollama 0.33 for chat models without an embedding runner
        return web.json_response({"error": "This server does not support embeddings. Start it with `--embeddings`"}, status=501)
    loaded.setdefault(m, (b.get("options") or {}).get("num_ctx") or 4096)
    print(json.dumps({"node": name, "path": req.path, "model": m, "inputs": len(inputs), "keep_alive": b.get("keep_alive")}), flush=True)
    return web.json_response({"model": m, "embeddings": [[0.1, 0.2, 0.3] for _ in inputs], "prompt_eval_count": 7 * len(inputs)})


@web.middleware
async def need_token(req, handler):
    if TOKEN and req.headers.get("X-Router-Token") != TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(req)


app = web.Application(middlewares=[need_token])
async def version(_):
    return web.json_response({"version": "0.0.0-fake"})


app.router.add_get("/api/tags", tags); app.router.add_get("/api/ps", ps); app.router.add_post("/api/show", show); app.router.add_get("/api/version", version)
app.router.add_post("/api/chat", infer); app.router.add_post("/api/generate", infer); app.router.add_post("/api/embed", embed)
sslctx = None
if TLS:
    import ssl
    sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sslctx.load_cert_chain(TLS[0], TLS[1])
web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None, ssl_context=sslctx)
