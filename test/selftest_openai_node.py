#!/usr/bin/env python3
"""Self-test: OpenAI-compatible nodes (openai_node.py; agent tunnel.upstream_api: openai, e.g. Strata).

A node whose agent fronts an OpenAI-compatible server instead of Ollama. Checks, against a fake server on loopback:
- the agent fact upstream_api reaches the node (registry -> Node.speaks_openai), and the node can switch back;
- the router's own poll reads /v1/models (not /api/tags + /api/ps) and fills models, loaded context, state; no /api/show;
- chat and generate go to /v1/chat/completions and come back as Ollama answers (text, tool calls, tokens), unary and
  streamed; max_tokens is sent next to max_completion_tokens;
- a server error arrives as CloudError with its status (proxy turns 404 into "next candidate");
- TunnelResponse offers iter_any / iter_chunked (the SSE reader needs them over the tunnel), also after line reads.
Runs without network beyond loopback, without Ollama and without an agent."""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from aiohttp import ClientSession, web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from skirnir_router import cloud, config, nodes, openai_node, poll, registry, state, tunnel  # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


MODEL = "big-moe"
SEEN = []   # requests the fake server received: (path, json body or None)


async def models(request):
    SEEN.append((request.path, None))
    return web.json_response({"object": "list", "data": [
        {"id": MODEL, "object": "model", "status": {"value": "loaded"}, "meta": {"n_ctx": 131072}},
        {"id": "cold-one", "object": "model", "status": {"value": "unloaded"}}]})


async def chat(request):
    body = await request.json()
    SEEN.append((request.path, body))
    if body.get("model") == "missing":
        return web.json_response({"error": {"message": "no such model", "code": "model_not_found"}}, status=404)
    call = {"id": "call_1", "type": "function", "function": {"name": "get_time", "arguments": "{\"zone\": \"UTC\"}"}}
    if not body.get("stream"):
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "Hello", "tool_calls": [call],
                                                           "reasoning_content": "Let me check."},
                                               "finish_reason": "tool_calls"}],
                                  "usage": {"prompt_tokens": 12, "completion_tokens": 3}})
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    events = [{"choices": [{"delta": {"role": "assistant", "content": ""}}]},
              {"choices": [{"delta": {"reasoning_content": "Hmm"}}]},
              {"choices": [{"delta": {"content": "Hel"}}]},
              {"choices": [{"delta": {"content": "lo"}}]},
              {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "get_time", "arguments": "{\"zone\""}}]}}]},
              {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": \"UTC\"}"}}]}, "finish_reason": "tool_calls"}]},
              {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}}]
    for ev in events:
        await resp.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


async def ollama_only(request):
    SEEN.append((request.path, None))
    return web.json_response({"error": "an OpenAI server has no Ollama API"}, status=404)


def check_spec_and_switch():
    entry = {"name": "strata-box", "facts": {"upstream_api": "openai", "vram_total_mib": 32 * 1024}, "policy": {}}
    spec = registry.node_spec_from_entry(entry)
    n = nodes.Node("strata-box", spec)
    check("agent fact upstream_api=openai -> node speaks OpenAI", n.speaks_openai and spec.get("upstream_api") == "openai")
    entry["facts"] = {"vram_total_mib": 32 * 1024}
    n.apply_spec(registry.node_spec_from_entry(entry))
    check("fact gone (agent back on Ollama) -> node speaks Ollama again", not n.speaks_openai)
    entry["facts"] = {"upstream_api": "vllm"}
    check("unknown value -> Ollama (only 'openai' switches)", "upstream_api" not in registry.node_spec_from_entry(entry))


def check_models_to_ollama():
    tags, ps = openai_node.models_to_ollama({"data": [{"id": "a", "meta": {"n_ctx": 4096}}, {"id": ""}, "junk",
                                                      {"id": "b", "status": {"value": "unloaded"}}, {"id": "c", "meta": {"n_ctx": True}}]})
    check("models_to_ollama: entries without id dropped, unloaded only installed",
          [t["name"] for t in tags] == ["a", "b", "c"] and [p["name"] for p in ps] == ["a", "c"], f"{tags} {ps}")
    check("... context_length from meta.n_ctx, booleans are no numbers, sizes 0",
          ps[0].get("context_length") == 4096 and "context_length" not in ps[1] and ps[0]["size_vram"] == 0)
    check("... no list at all -> empty", openai_node.models_to_ollama({"data": "x"}) == ([], []) and openai_node.models_to_ollama(None) == ([], []))


async def check_tunnel_iterators():
    def frames(*parts):
        r = tunnel.TunnelResponse(None, 1, None)
        for p in parts:
            r.q.put_nowait((tunnel.TUN_DATA, p))
        r.q.put_nowait((tunnel.TUN_END, b""))
        return r
    r = frames(b"line1\npart", b"ial-more", b"")
    first = await r.__anext__()
    rest = [c async for c in r.iter_any()]
    check("TunnelResponse.iter_any: after a line read, the buffered rest comes first, empty frames skipped",
          first == b"line1\n" and rest == [b"part", b"ial-more"], f"{first} {rest}")
    r = frames(b"abcdefg", b"hi")
    pieces = [c async for c in r.iter_chunked(3)]
    check("TunnelResponse.iter_chunked: pieces of at most n bytes", pieces == [b"abc", b"def", b"g", b"hi"], str(pieces))


async def main():
    state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
    state.CFG.prewarm_on_online = False
    check_spec_and_switch()
    check_models_to_ollama()
    await check_tunnel_iterators()

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    for p in ("/api/tags", "/api/ps", "/api/show", "/api/version"):
        app.router.add_route("*", p, ollama_only)
    server = TestServer(app)
    await server.start_server()
    previous = state.SESSION
    state.SESSION = ClientSession()
    try:
        n = nodes.Node("strata-box", {"ollama": str(server.make_url("")).rstrip("/"), "upstream_api": "openai"})
        await poll.poll_node(n)
        await asyncio.sleep(0)   # let spawned tasks (none expected) run
        paths = [p for p, _ in SEEN]
        check("poll: /v1/models only, no Ollama endpoints (/api/tags, /api/ps, /api/show, /api/version)",
              paths == ["/v1/models"], str(paths))
        check("poll: installed models from the list, the loaded one with its context",
              n.models == {MODEL, "cold-one"} and n.loaded_ctx.get(f"openai:{MODEL}") == 131072 and n.is_loaded(MODEL), f"{n.models} {n.loaded_ctx}")
        check("poll: node online (free), loaded size 0 (memory counts once, as agent child)",
              n.state == "free" and n.loaded.get(MODEL) == 0, f"{n.state} {n.loaded}")
        check("poll: no capabilities fetch for an OpenAI node", MODEL not in state.CAPS)

        body = {"model": MODEL, "messages": [{"role": "user", "content": "time?"}],
                "tools": [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}],
                "options": {"num_predict": 64, "num_ctx": 8192}, "keep_alive": "5m"}
        SEEN.clear()
        info, j = await openai_node.run(n, "/api/chat", body, MODEL, False)
        _, sent = SEEN[-1]
        check("unary: request went to /v1/chat/completions with the model name as reported", SEEN[-1][0] == "/v1/chat/completions" and sent["model"] == MODEL)
        check("unary: max_tokens next to max_completion_tokens, no Ollama-only fields",
              sent.get("max_tokens") == 64 and sent.get("max_completion_tokens") == 64 and "options" not in sent and "keep_alive" not in sent, str(sent))
        msg = j.get("message") or {}
        check("unary: Ollama answer with text, tool call (arguments as object) and token counts",
              j.get("done") and msg.get("content") == "Hello" and msg.get("tool_calls", [{}])[0]["function"]["arguments"] == {"zone": "UTC"}
              and j["prompt_eval_count"] == 12 and j["eval_count"] == 3 and info["finish"] == "tool_calls", json.dumps(j)[:300])
        check("unary: the server's reasoning_content becomes Ollama's thinking", msg.get("thinking") == "Let me check.", str(msg))
        check("think absent -> no reasoning_effort sent", "reasoning_effort" not in sent)

        await openai_node.run(n, "/api/chat", {**body, "think": False}, MODEL, False)
        check("think: false -> reasoning_effort none (else a thinking model spends the budget on thinking)",
              SEEN[-1][1].get("reasoning_effort") == "none", str(SEEN[-1][1]))

        info, chunks = await openai_node.run(n, "/api/chat", {**body, "stream": True}, MODEL, True)
        got = [c async for c in chunks]
        text = "".join((c.get("message") or {}).get("content", "") for c in got if not c.get("done"))
        thought = "".join((c.get("message") or {}).get("thinking", "") for c in got if not c.get("done"))
        last = got[-1]
        check("stream: thinking and text chunks in Ollama form, then one done chunk",
              text == "Hello" and thought == "Hmm" and last.get("done") and len(got) == 4, f"{len(got)} {text!r} {thought!r}")
        check("stream: tool call assembled from deltas, tokens from the usage event",
              last["message"]["tool_calls"][0]["function"]["arguments"] == {"zone": "UTC"} and last["eval_count"] == 5 and info["p_tok"] == 12,
              json.dumps(last)[:300])

        _, j = await openai_node.run(n, "/api/generate", {"model": MODEL, "prompt": "Say hi", "options": {}}, MODEL, False)
        check("generate: prompt sent as a user message, answer as `response`",
              SEEN[-1][1]["messages"][-1] == {"role": "user", "content": "Say hi"} and j.get("response") == "Hello", json.dumps(SEEN[-1][1])[:200])

        status = None
        try:
            await openai_node.run(n, "/api/chat", {**body, "model": "missing"}, "missing", False)
        except cloud.CloudError as e:  # exception-ok: the adapter's error channel is the subject of this check
            status, unavailable = e.status, e.model_unavailable
        check("server 404 model_not_found -> CloudError 404, model unavailable (proxy: next candidate)", status == 404 and unavailable)
        check("OpenAI paths: chat and generate only (embeddings stay on Ollama nodes)", openai_node.PATHS == ("/api/chat", "/api/generate"))
    finally:
        await state.SESSION.close()
        state.SESSION = previous
        await server.close()


asyncio.run(main())
print(f"\n{'ALLE OK' if not FAILS else str(len(FAILS)) + ' FEHLER: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
