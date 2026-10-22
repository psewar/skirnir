#!/usr/bin/env python3
"""Self-test (Router 0.3.7): a role request with a smaller num_ctx reuses the context the model is already loaded
with - provided it falls within the tier. Trigger 2026-09-30: qwen3.8:27b does not share the runner across different num_ctx
values (unlike qwen3.6); a client with 65536 alongside one with 131072 reloaded on every switch (10.4 s / 7.4 s measured).
Runs without Router, network, or Ollama."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import proxy, state   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class FakeNode:
    def __init__(self, loaded):
        self.loaded, self.state = loaded, "free"

    def loaded_context(self, model):
        return self.loaded.get(model)


class Cfg:
    roles = {"assist:latest": {}}
    keep_alive = {"free": -1}


state.CFG = Cfg()
ROLE = {"exposed": "assist:latest"}
CONCRETE = {"exposed": "qwen3.8:27b"}
TIER = {"model": "qwen3.8:27b", "num_ctx": 131072}
BODY = {"model": "assist:latest", "options": {"num_ctx": 65536}}

out, ctx = proxy._backend_body(BODY, ROLE, TIER, 65536, FakeNode({"qwen3.8:27b": 131072}))
check("Role, model with 131k loaded, Client 65k -> 131k (no reload)", ctx == 131072 and out["options"]["num_ctx"] == 131072, ctx)

out, ctx = proxy._backend_body(BODY, ROLE, TIER, 65536, FakeNode({}))
check("Role, model not loaded -> Client context 65k (no unnecessary VRAM)", ctx == 65536, ctx)

out, ctx = proxy._backend_body(BODY, ROLE, TIER, 65536, FakeNode({"qwen3.8:27b": 32768}))
check("Role, loaded with smaller context -> 65k (reload needed and correct)", ctx == 65536, ctx)

out, ctx = proxy._backend_body(BODY, ROLE, {"model": "qwen3.8:27b", "num_ctx": 65536}, 65536, FakeNode({"qwen3.8:27b": 131072}))
check("Role, loaded larger than the tier allows -> Tier remains upper limit (65k)", ctx == 65536, ctx)

out, ctx = proxy._backend_body({"model": "qwen3.8:27b", "options": {"num_ctx": 65536}}, CONCRETE,
                               {"model": "qwen3.8:27b", "num_ctx": 65536}, 65536, FakeNode({"qwen3.8:27b": 131072}))
check("specific model with own num_ctx -> stays at Client request", ctx == 65536, ctx)

out, ctx = proxy._backend_body(BODY, ROLE, TIER, 131072, FakeNode({"qwen3.8:27b": 131072}))
check("Role, same context -> unchanged", ctx == 131072, ctx)

print(f"\n{'ALLE GRUEN' if not FAILS else f'{len(FAILS)} FEHLER: {FAILS}'}")
sys.exit(1 if FAILS else 0)
