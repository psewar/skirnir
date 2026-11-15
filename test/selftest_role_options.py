#!/usr/bin/env python3
"""Self-test (Router 0.30.0, ADR-0093): a role's sampling defaults (`roles.<r>.options`).
- checked when the configuration loads: only known sampling keys, right type, within range;
- applied to the role's local tiers for what the client leaves out; the client's own value wins; a cloud tier and a
  request for a concrete model get nothing;
- reach an OpenAI-compatible node (llama.cpp, Strata) including top_k, min_p and the penalties; a cloud provider gets
  only what it took before.
Trigger: the same voice question failed in 7 of 12 runs on a node that samples with its model card's defaults when the
client sends no temperature; with temperature 0 it answered 6 of 6. Runs without router, network or Ollama."""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import proxy  # noqa: E402,F401  (load order as in the router)
from skirnir_router import cloud, config, state  # noqa: E402
from skirnir_router.outcome import failed  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "router", "config.example.yaml")
FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


# --- validation ---------------------------------------------------------------------------------------------------
ok = config.role_options("assist", {"temperature": 0, "top_k": 20, "presence_penalty": 1.5})
check("valid options are taken, an integer temperature becomes a number", ok == {"temperature": 0.0, "top_k": 20, "presence_penalty": 1.5}, ok)
check("no options = empty", config.role_options("assist", None) == {})
for raw, why in (({"num_ctx": 8192}, "num_ctx is no sampling option (the tier sets it)"),
                 ({"temprature": 0.2}, "a typo is refused, not ignored"),
                 ({"temperature": True}, "a boolean is no number"),
                 ({"temperature": "0.2"}, "a string is no number"),
                 ({"top_k": 20.0}, "top_k must be an integer"),
                 ({"temperature": 3}, "temperature above 2"),
                 ({"top_p": -0.1}, "top_p below 0"),
                 ([("temperature", 0)], "options must be a mapping")):
    p = config.role_options("assist", raw)
    check(f"refused: {why}", failed(p) and p.status == 400, getattr(p, "message", p))

# --- configuration: base config.yaml and the UI/API override both carry options ------------------------------------
cfg = config.Config.load(EXAMPLE, use_overrides=False)
check("example configuration loads", not failed(cfg), getattr(cfg, "message", ""))
check("a role without options has an empty dict", cfg.roles["assist:latest"]["options"] == {})

def override(assist):
    """roles.yaml as the UI writes it: every role (it replaces the set), here only assist with something of its own."""
    return {"roles": {name: (assist if name == "assist" else {}) for name in config.load_yaml(EXAMPLE)["roles"]}}


p = cfg.reload(override({"options": {"temperature": 0.1, "top_k": 20}}))
check("override with options is taken", p is None and cfg.roles["assist:latest"]["options"] == {"temperature": 0.1, "top_k": 20},
      getattr(p, "message", cfg.roles["assist:latest"]["options"]))
check("the override keeps the base tiers and priority", cfg.roles["assist:latest"]["priority"] == "interactive"
      and len(cfg.roles["assist:latest"]["tiers"]) == 4)
p = cfg.reload(override({"options": {"temperature": 9}}))
check("an invalid override is refused and nothing changes", failed(p) and cfg.roles["assist:latest"]["options"] == {"temperature": 0.1, "top_k": 20},
      getattr(p, "message", p))
state.CFG = cfg
view = config.roles_as_config()["assist"]["options"]
check("GET /admin/config shows the options", view == {"temperature": 0.1, "top_k": 20}, view)


# --- applied per tier ----------------------------------------------------------------------------------------------
class FakeNode:
    state = "free"

    def loaded_context(self, model):
        return None


ROLE = cfg.roles["assist:latest"]
LOCAL = {"model": "qwen3.6:35b-a3b", "num_ctx": 65536}
CLOUD = {"model": "openai:gpt-5-mini", "num_ctx": 128000, "cloud": "openai"}
out, _ = proxy._backend_body({"model": "assist:latest", "options": {"num_ctx": 8192}}, ROLE, LOCAL, 8192, FakeNode())
check("local tier gets the role's defaults", out["options"].get("temperature") == 0.1 and out["options"].get("top_k") == 20, out["options"])
out, _ = proxy._backend_body({"model": "assist:latest", "options": {"temperature": 0.9}}, ROLE, LOCAL, 8192, FakeNode())
check("the client's own temperature wins, the rest is filled in", out["options"]["temperature"] == 0.9 and out["options"]["top_k"] == 20, out["options"])
out, _ = proxy._backend_body({"model": "assist:latest", "options": {}}, ROLE, CLOUD, 8192, FakeNode())
check("cloud tier gets no role defaults (gpt-5 refuses a temperature)", "temperature" not in out["options"] and "top_k" not in out["options"], out["options"])


class FakeCloud(FakeNode):
    is_cloud = True


out, _ = proxy._backend_body({"model": "assist:latest", "options": {}}, ROLE, {"model": "openai:gpt-5-mini", "num_ctx": 128000,
                                                                          "canary": True}, 8192, FakeCloud())
check("rejected case: a canary tier on a cloud provider (no `cloud` key) gets no role defaults",
      "temperature" not in out["options"], out["options"])
out, _ = proxy._backend_body({"model": "qwen3.6:35b-a3b", "options": {}}, {"exposed": "qwen3.6:35b-a3b", "options": {"temperature": 0.1}},
                             LOCAL, 8192, FakeNode())
check("a concrete model gets no role defaults", "temperature" not in out["options"], out["options"])


# --- what an OpenAI-compatible node and a cloud provider receive ---------------------------------------------------
class FakeResponse:
    status = 200

    async def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def sent(local_server, options=None):
    seen = {}

    def post(req):
        seen.update(req)
        return FakeResponse()
    body = {"messages": [{"role": "user", "content": "hi"}], "think": False,
            "options": options or {"temperature": 0.0, "top_p": 0.8, "top_k": 20, "min_p": 0.05, "presence_penalty": 1.5,
                                   "frequency_penalty": 0.2, "repeat_penalty": 1.1}}
    asyncio.run(cloud.openai_exchange(post, "/api/chat", body, "m", "m", False, local_server=local_server))
    return seen


node = sent(True)
check("node gets temperature and top_p as before", node.get("temperature") == 0.0 and node.get("top_p") == 0.8, node)
check("node gets top_k, min_p, presence and frequency penalty", node.get("top_k") == 20 and node.get("min_p") == 0.05
      and node.get("presence_penalty") == 1.5 and node.get("frequency_penalty") == 0.2, node)
check("repeat_penalty reaches the node as repetition_penalty", node.get("repetition_penalty") == 1.1 and "repeat_penalty" not in node, node)
odd = sent(True, {"top_k": "20; drop", "min_p": {"x": 1}, "presence_penalty": True, "frequency_penalty": [1]})
check("rejected case: a client's non-numeric sampling values never reach the node", not any(
    k in odd for k in ("top_k", "min_p", "presence_penalty", "frequency_penalty")), odd)
prov = sent(False)
check("cloud provider gets no top_k/min_p/penalties (unchanged)", not any(k in prov for k in ("top_k", "min_p", "presence_penalty",
                                                                                              "frequency_penalty", "repetition_penalty")), prov)

print(f"\n{'ALL GREEN' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
sys.exit(1 if FAILS else 0)
