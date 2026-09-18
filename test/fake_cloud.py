#!/usr/bin/env python3
"""Fake-Cloud fuer die Tests (Stufe 5): OpenAI-kompatibel (/v1/chat/completions, /v1/embeddings) und Anthropic
(/v1/messages), beide mit Stream. Prueft den API-Schluessel, antwortet mit festen Texten und usage.
Aufruf: fake_cloud.py <port> <openai-key> <anthropic-key>"""
import asyncio
import json
import sys

from aiohttp import web

port, OA_KEY, AN_KEY = int(sys.argv[1]), sys.argv[2], sys.argv[3]
seen = []


def log(kind, b):
    seen.append({"kind": kind, "model": b.get("model"), "stream": bool(b.get("stream")), "tools": bool(b.get("tools")),
                 "n_msgs": len(b.get("messages") or [])})
    print(json.dumps(seen[-1]), flush=True)


async def chat(req):
    if req.headers.get("Authorization") != f"Bearer {OA_KEY}":
        return web.json_response({"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}, status=401)
    b = await req.json()
    log("openai", b)
    if b.get("model") == "kaputt":
        return web.json_response({"error": {"message": "The server had an error", "type": "server_error"}}, status=500)
    if b.get("model", "").endswith("nicht"):
        return web.json_response({"error": {"message": f"The model `{b['model']}` does not exist", "type": "invalid_request_error"}}, status=404)
    usage = {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}
    tc = [{"id": "call_abc", "type": "function", "function": {"name": "get_time", "arguments": "{\"tz\": \"CET\"}"}}] if b.get("tools") else None
    if not b.get("stream"):
        msg = {"role": "assistant", "content": None if tc else "Hallo aus der Wolke"}
        if tc:
            msg["tool_calls"] = tc
        return web.json_response({"id": "chatcmpl-fake", "object": "chat.completion", "model": b["model"],
                                  "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tc else "stop"}], "usage": usage})
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(req)

    async def send(obj):
        await resp.write(("data: " + json.dumps(obj) + "\n\n").encode())
    base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": b["model"]}
    if tc:
        await send({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_abc", "type": "function", "function": {"name": "get_time", "arguments": "{\"tz\": "}}]}, "finish_reason": None}]})
        await send({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"CET\"}"}}]}, "finish_reason": None}]})
        await send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    else:
        for tok in ["Hallo ", "aus der ", "Wolke"]:
            await asyncio.sleep(0.02)
            await send({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": tok}, "finish_reason": None}]})
        await send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    await send({**base, "choices": [], "usage": usage})
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def embeddings(req):
    if req.headers.get("Authorization") != f"Bearer {OA_KEY}":
        return web.json_response({"error": {"message": "Incorrect API key provided"}}, status=401)
    b = await req.json()
    log("openai-embed", b)
    inp = b.get("input")
    n = len(inp) if isinstance(inp, list) else 1
    return web.json_response({"object": "list", "data": [{"object": "embedding", "index": i, "embedding": [0.5, 0.25, 0.125]} for i in range(n)],
                              "model": b.get("model"), "usage": {"prompt_tokens": 7 * n, "total_tokens": 7 * n}})


async def messages(req):
    if req.headers.get("x-api-key") != AN_KEY or not req.headers.get("anthropic-version"):
        return web.json_response({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, status=401)
    b = await req.json()
    log("anthropic", b)
    usage = {"input_tokens": 25, "output_tokens": 35}
    tools = bool(b.get("tools"))
    if not b.get("stream"):
        content = ([{"type": "tool_use", "id": "toolu_01", "name": "get_time", "input": {"tz": "CET"}}] if tools
                   else [{"type": "text", "text": "Hallo von Claude"}])
        return web.json_response({"id": "msg_fake", "type": "message", "role": "assistant", "model": b["model"], "content": content,
                                  "stop_reason": "tool_use" if tools else "end_turn", "usage": usage})
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(req)

    async def ev(t, obj):
        await resp.write((f"event: {t}\ndata: " + json.dumps({"type": t, **obj}) + "\n\n").encode())
    await ev("message_start", {"message": {"id": "msg_fake", "role": "assistant", "model": b["model"], "usage": {"input_tokens": 25}}})
    if tools:
        await ev("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "toolu_01", "name": "get_time", "input": {}}})
        await ev("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"tz\": "}})
        await ev("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": "\"CET\"}"}})
        await ev("content_block_stop", {"index": 0})
        await ev("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 35}})
    else:
        await ev("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
        for tok in ["Hallo ", "von ", "Claude"]:
            await asyncio.sleep(0.02)
            await ev("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": tok}})
        await ev("content_block_stop", {"index": 0})
        await ev("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 35}})
    await ev("message_stop", {})
    await resp.write_eof()
    return resp


app = web.Application()
app.router.add_post("/v1/chat/completions", chat)
app.router.add_post("/v1/embeddings", embeddings)
app.router.add_post("/v1/messages", messages)
web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None)
