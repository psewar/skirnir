#!/usr/bin/env python3
"""Self-test (Router 0.33.0, ADR-0098): which value turns a cloud model's reasoning off, and its thought in the answer.
- The catalog key `reasoning_off` (minimal | none) is the reasoning_effort sent for `think: false` on a cloud model with
  `reasoning: true`; without the key it stays `minimal` (OpenAI's gpt-5 models). `think: true`, no `think` and a model
  without `reasoning: true` send none.
- The configuration check refuses an unknown value, the key without `reasoning: true` and the key on a local model;
  PUT /admin/config's cloud entry keeps the key, and its writer refuses an invalid value without writing.
- The provider's thought arrives as Ollama's `thinking` from `reasoning` (vLLM-style servers) or `reasoning_content`
  (llama.cpp-style), unary and streamed, never doubled when a server sends both, and not for a client that sent
  `think: false` (also when the model thinks anyway).
Trigger: a vLLM-served reasoning model ignored `minimal` and kept thinking (a small max_tokens ended with empty content);
`none` turns it off. Its thought came as `reasoning`, which the cloud path dropped. Requests go through cloud.run to a fake
OpenAI-compatible provider in this process; no network, router or Ollama. Synthetic names only."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import proxy  # noqa: E402,F401  (load order as in the router)
from skirnir_router import admin, cloud, config, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
GPT = "openai:gpt-5-mini"                    # example catalog: reasoning: true, no reasoning_off
REASONER = "openai:example-reasoner"         # added below: reasoning: true, reasoning_off: none
PLAIN = "openai:example-plain"               # added below: no reasoning
THOUGHT, ANSWER = "Synthetic thought about the question.", "Synthetic answer."
FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


# --- fake OpenAI-compatible provider ---------------------------------------------------------------------------------
class Provider:
    """Answers /chat/completions like a reasoning model behind vLLM: it thinks unless reasoning_effort is "none" (it
    ignores "minimal", as measured). `fields` names where the thought goes: reasoning (vLLM), reasoning_content
    (llama.cpp) or both with the same text."""

    def __init__(self):
        self.sent = []
        self.fields = ("reasoning",)

    def thinks(self, req):
        return req.get("reasoning_effort") != "none"

    def unary(self, req):
        msg = {"role": "assistant", "content": ANSWER}
        if self.thinks(req):
            msg.update(dict.fromkeys(self.fields, THOUGHT))
        return {"id": "chatcmpl-synthetic", "object": "chat.completion", "model": req["model"],
                "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 30, "total_tokens": 42}}

    def events(self, req):
        out = []
        if self.thinks(req):
            half = len(THOUGHT) // 2
            out += [{"choices": [{"index": 0, "delta": dict.fromkeys(self.fields, part)}]} for part in (THOUGHT[:half], THOUGHT[half:])]
        out.append({"choices": [{"index": 0, "delta": {"content": ANSWER}, "finish_reason": "stop"}]})
        out.append({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 30, "total_tokens": 42}})
        return out


class Content:
    def __init__(self, raw):
        self.raw = raw

    async def iter_any(self):
        for i in range(0, len(self.raw), 17):   # split mid-line, as a network read can
            yield self.raw[i:i + 17]


class Response:
    def __init__(self, req, provider):
        self.status = 200
        self.req, self.provider = req, provider
        events = provider.events(req) if req.get("stream") else []
        self.content = Content(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n")

    async def read(self):
        return json.dumps(self.provider.unary(self.req)).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Session:
    """Stands in for state.SESSION: records each request body, answers from the fake provider."""

    def __init__(self, provider):
        self.provider = provider

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=True):
        self.provider.sent.append(json)
        return Response(json, self.provider)


TARGET = SimpleNamespace(provider="openai", api_key="synthetic-key", spec={"kind": "openai", "base_url": "https://llm.example.org/v1"})


async def ask(model, path="/api/chat", stream=False, **extra):
    body = {"model": model, **extra}
    if path == "/api/generate":
        body["prompt"] = "Synthetic question?"
    else:
        body["messages"] = [{"role": "user", "content": "Synthetic question?"}]
    _, answer = await cloud.run(TARGET, path, body, model, stream)
    if not stream:
        return answer
    thinking, content, last = "", "", None
    async for piece in answer:
        part = piece.get("message") or {}
        thinking += part.get("thinking") or piece.get("thinking") or ""
        content += part.get("content") or piece.get("response") or ""
        last = piece
    return {"thinking": thinking, "content": content, "done": bool(last and last.get("done"))}


def sent_effort(provider):
    return provider.sent[-1].get("reasoning_effort", "<absent>")


# --- configuration --------------------------------------------------------------------------------------------------
cfg = config.Config.load(EXAMPLE, use_overrides=False)
check("example configuration loads", not failed(cfg), getattr(cfg, "message", ""))
check("instrument: the example's gpt-5-mini reasons and names no reasoning_off",
      cfg.models[GPT].get("reasoning") is True and "reasoning_off" not in cfg.models[GPT], cfg.models.get(GPT))

EXTRA = {REASONER: {"cloud": "openai", "provider_model": "example-org/reasoner-small", "reasoning": True, "reasoning_off": "none",
                    "price_chf_per_m": {"input": 0, "output": 0}},
         PLAIN: {"cloud": "openai", "provider_model": "example-plain", "price_chf_per_m": {"input": 0, "output": 0}}}
p = cfg.reload({"models": EXTRA})
check("reasoning_off: none on a reasoning cloud model is accepted", p is None and cfg.models[REASONER]["reasoning_off"] == "none",
      getattr(p, "message", p))
p = cfg.reload({"models": {**EXTRA, GPT: {"reasoning_off": "minimal"}}})
check("reasoning_off: minimal is accepted and merges into the config.yaml entry",
      p is None and cfg.models[GPT].get("reasoning_off") == "minimal" and cfg.models[GPT].get("cloud") == "openai", getattr(p, "message", p))

for entry, why in (({**EXTRA[REASONER], "reasoning_off": "low"}, "a value that does not turn reasoning off"),
                   ({**EXTRA[REASONER], "reasoning_off": "None"}, "values are lower case, as the provider takes them"),
                   ({**EXTRA[REASONER], "reasoning_off": True}, "a boolean is no value"),
                   ({**EXTRA[REASONER], "reasoning_off": ["none"]}, "a list is no value"),
                   ({**EXTRA[PLAIN], "reasoning_off": "none"}, "the key without reasoning: true would do nothing"),
                   ({**EXTRA[REASONER], "reasoning": False}, "reasoning: false with the key would do nothing"),
                   ({"weights_gib": 4.0, "kv_gib_per_1k": 0.1, "reasoning": True, "reasoning_off": "none"},
                    "a local model never uses it (an OpenAI-compatible node always gets none)")):
    p = cfg.reload({"models": {**EXTRA, "example-checked": entry}})
    check(f"refused: {why}", failed(p) and p.status == 400 and "reasoning_off" in p.message, getattr(p, "message", p))
check("a refused configuration changes nothing", "example-checked" not in cfg.models and cfg.models[REASONER]["reasoning_off"] == "none")

state.CFG = cfg
entry = admin._cloud_model_entry(REASONER, {**EXTRA[REASONER]})
check("PUT /admin/config's cloud entry keeps reasoning_off as given (the configuration check judges it)",
      not failed(entry) and entry.get("reasoning_off") == "none" and entry.get("reasoning") is True, entry)
entry = admin._cloud_model_entry(PLAIN, {**EXTRA[PLAIN]})
check("... and adds none when the request has none", not failed(entry) and "reasoning_off" not in entry, entry)
with tempfile.TemporaryDirectory() as tmp:   # the writer behind PUT /admin/config, on a copy so nothing real is touched
    shutil.copy(EXAMPLE, os.path.join(tmp, "config.yaml"))
    state.CFG = config.Config.load(os.path.join(tmp, "config.yaml"), use_overrides=True)
    bad = {**EXTRA[REASONER], "reasoning_off": "low"}
    p = config.write_overrides({"models": {REASONER: admin._cloud_model_entry(REASONER, bad)}})
    check("PUT /admin/config with an invalid reasoning_off is refused with 400 and writes nothing",
          failed(p) and p.status == 400 and not os.path.exists(state.CFG.overrides_path), getattr(p, "message", p))
state.CFG = cfg


# --- request: which reasoning_effort goes out ------------------------------------------------------------------------
async def requests():
    provider = Provider()
    state.SESSION = Session(provider)
    await ask(GPT, think=False)
    check("think: false on gpt-5-mini without reasoning_off -> minimal (unchanged)", sent_effort(provider) == "minimal", provider.sent[-1])
    await ask(REASONER, think=False)
    check("think: false on a model with reasoning_off: none -> none", sent_effort(provider) == "none", provider.sent[-1])
    check("... with the provider's model name", provider.sent[-1]["model"] == "example-org/reasoner-small", provider.sent[-1]["model"])
    await ask(REASONER, path="/api/generate", think=False)
    check("... also for /api/generate", sent_effort(provider) == "none", provider.sent[-1])
    await ask(REASONER, stream=True, think=False)
    check("... also when streaming", sent_effort(provider) == "none", provider.sent[-1])
    await ask(REASONER, think=True)
    check("think: true -> no reasoning_effort (provider default)", sent_effort(provider) == "<absent>", provider.sent[-1])
    await ask(REASONER)
    check("no think -> no reasoning_effort", sent_effort(provider) == "<absent>", provider.sent[-1])
    await ask(PLAIN, think=False)
    check("a model without reasoning: true gets no reasoning_effort", sent_effort(provider) == "<absent>", provider.sent[-1])


# --- answer: the provider's thought as Ollama's thinking -------------------------------------------------------------
async def answers():
    provider = Provider()
    state.SESSION = Session(provider)
    for fields, style in ((("reasoning",), "reasoning (vLLM)"), (("reasoning_content",), "reasoning_content (llama.cpp)"),
                          (("reasoning_content", "reasoning"), "both fields with the same text")):
        provider.fields = fields
        j = await ask(REASONER)
        msg = j.get("message") or {}
        check(f"unary chat, {style}: thought as thinking, answer as content",
              msg.get("thinking") == THOUGHT and msg.get("content") == ANSWER and j.get("done") is True, msg)
        s = await ask(REASONER, stream=True)
        check(f"streamed chat, {style}: thinking pieces add up to the thought once",
              s == {"thinking": THOUGHT, "content": ANSWER, "done": True}, s)
    provider.fields = ("reasoning",)
    j = await ask(REASONER, path="/api/generate", think=True)
    check("unary generate with think: true: thinking next to response",
          j.get("thinking") == THOUGHT and j.get("response") == ANSWER, j)
    s = await ask(REASONER, path="/api/generate", stream=True)
    check("streamed generate: thinking and response", s == {"thinking": THOUGHT, "content": ANSWER, "done": True}, s)

    # gpt-5-mini gets minimal, which this fake model ignores like the measured one: it thinks anyway
    for fields in (("reasoning",), ("reasoning_content",)):
        provider.fields = fields
        j = await ask(GPT, think=False)
        msg = j.get("message") or {}
        check(f"instrument: the fake provider thought despite minimal ({fields[0]})",
              sent_effort(provider) == "minimal" and provider.thinks(provider.sent[-1]))
        check(f"think: false -> no thinking in the answer, also when the model thought anyway ({fields[0]})",
              "thinking" not in msg and msg.get("content") == ANSWER, msg)
        s = await ask(GPT, stream=True, think=False)
        check(f"... also when streaming ({fields[0]})", s == {"thinking": "", "content": ANSWER, "done": True}, s)

    provider.fields = ()
    j = await ask(REASONER)
    check("no thought from the provider -> no thinking field", "thinking" not in (j.get("message") or {}), j.get("message"))


# --- an OpenAI-compatible node shares the adapter -------------------------------------------------------------------
async def node():
    provider = Provider()
    session = Session(provider)

    def post(req):
        return session.post("https://llm.example.org/v1/chat/completions", json=req)
    _, j = await cloud.openai_exchange(post, "/api/chat", {"messages": [{"role": "user", "content": "Synthetic question?"}]},
                                       "example-node-model", "example-node-model", False, local_server=True)
    check("an OpenAI-compatible node's `reasoning` also becomes thinking", (j.get("message") or {}).get("thinking") == THOUGHT, j.get("message"))
    await cloud.openai_exchange(post, "/api/chat", {"messages": [{"role": "user", "content": "x"}], "think": False},
                                "example-node-model", "example-node-model", False, local_server=True)
    check("... and think: false on a node still sends none (router 0.10.0)", sent_effort(provider) == "none", provider.sent[-1])


asyncio.run(requests())
asyncio.run(answers())
asyncio.run(node())
print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
