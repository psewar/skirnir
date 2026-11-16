#!/usr/bin/env python3
"""Self-test (Router 0.32.2, issue #162): a poll answer that arrives after the agent's report is dropped.

Trigger 2026-11-16: twice, a request for the model of a node's held OpenAI-compatible server got 503 "no node has this
model" in the first 30 s after a router restart. The poll loop and the agent's tunnel start together; a poll that went out
before the agent's first OLLAMA report (standby models plus the held server's) and came back after it replaced that list
with what the poll saw - the standby answering /v1/models with its own models only - until the agent's next report 30 s
later. Runs without network, agent or server."""
import asyncio
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "router"))
from skirnir_router import config, nodes, openai_node, poll, registry, state  # noqa: E402

FAILS = []
SERVER, SMALL = "big-moe", "small:8b"
SERVER_TAG = {"name": SERVER, "digest": f"openai:{SERVER}", "size": 0}
STANDBY_TAGS = [{"name": SMALL, "digest": "sha256:aa", "size": 5 << 30}]


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def push(n):
    """The agent's report while its server is held and the standby serves: the standby's models plus the server's."""
    poll.apply_ollama_push(n, {"up": True, "rev": 1, "tags": STANDBY_TAGS + [SERVER_TAG], "ps": [],
                               "upstream_state": "held", "serving": "ollama"}, time.time())


class Answer:
    def __init__(self, body):
        self.body = body

    async def json(self, content_type=None):
        return self.body


class InFlight:
    """nreq stand-in: the agent's report arrives while the request is in flight (`push_during`), then the answer comes."""

    def __init__(self, n, body, push_during, fail=False):
        self.n, self.body, self.push_during, self.fail = n, body, push_during, fail

    async def __aenter__(self):
        await asyncio.sleep(0.01)
        if self.push_during:
            push(self.n)
        await asyncio.sleep(0.01)
        if self.fail:
            # exception-ok: stands in for the network error a real tunnel request raises at this boundary
            raise OSError("tunnel request failed")
        return Answer(self.body)

    async def __aexit__(self, *exc):
        return False


def fresh_node():
    """A node as the router creates it from the register after a restart: facts known, nothing reported yet."""
    facts = {"upstream_api": "openai", "upstream_release": True, "upstream_fallback": True}
    spec = registry.node_spec_from_entry({"name": "strata-box", "facts": facts, "policy": {}})
    n = nodes.Node("strata-box", {**spec, "vram_total_gib": 32})
    n.tunnel = object()
    return n


async def main():
    state.CFG = config.Config(os.path.join(HERE, "..", "router", "config.example.yaml"), use_overrides=False)
    state.CFG.mqtt = None
    state.CFG.prewarm_on_online = False
    state.spawn = lambda coro: coro.close()
    poll.evaluate = lambda node, now: None
    standby_v1 = {"data": [{"id": SMALL}]}   # what the standby Ollama answers on /v1/models: its own models only

    # 0) The instrument: a fresh node polls the OpenAI way, and a poll answer alone loses the held server's model
    n = fresh_node()
    check("setup: fresh node speaks OpenAI until the agent reports the standby", n.speaks_openai)
    poll.apply_ollama_state(n, *openai_node.models_to_ollama(standby_v1), time.time())
    check("setup: applying that poll answer drops the server's model (the observed state)", SERVER not in n.models, str(n.models))

    # 1) The race: the poll goes out, the agent's report arrives, then the poll answer - the report must stand
    n = fresh_node()
    real_nreq = nodes.nreq
    nodes.nreq = lambda node, method, path, **kw: InFlight(node, standby_v1, push_during=True)
    try:
        await poll.poll_node(n)
    finally:
        nodes.nreq = real_nreq
    check("poll answer after the agent's report is dropped: the held server's model stays offered",
          SERVER in n.models and n.offers(SERVER) and n.releases_server_for(SERVER), str(sorted(n.models)))
    check("the standby's model stays listed with its own digest", n.digest_of.get(SMALL) == "sha256:aa", str(n.digest_of))

    # 2) A poll that fails after the agent's report does not count as a miss
    n = fresh_node()
    nodes.nreq = lambda node, method, path, **kw: InFlight(node, standby_v1, push_during=True, fail=True)
    try:
        await poll.poll_node(n)
    finally:
        nodes.nreq = real_nreq
    check("failed poll after the agent's report: no miss counted", n.misses == 0 and SERVER in n.models, f"misses {n.misses}")

    # 3) Without a report in between the poll applies as before (older agents, static nodes)
    n = fresh_node()
    n.openai_upstream = False   # a plain Ollama node: /api/tags and /api/ps
    tags_ps = {"/api/tags": {"models": STANDBY_TAGS}, "/api/ps": {"models": []}}
    nodes.nreq = lambda node, method, path, **kw: InFlight(node, tags_ps[path], push_during=False)
    try:
        await poll.poll_node(n)
    finally:
        nodes.nreq = real_nreq
    check("no report in between: the poll answer is applied", n.models == {SMALL} and n.misses == 0, str(n.models))

    # 4) A failed poll without a report in between still counts as a miss
    n = fresh_node()
    nodes.nreq = lambda node, method, path, **kw: InFlight(node, standby_v1, push_during=False, fail=True)
    try:
        await poll.poll_node(n)
    finally:
        nodes.nreq = real_nreq
    check("failed poll without a report: one miss", n.misses == 1, f"misses {n.misses}")

    # 5) 0.32.3: the journal names where a changed model list came from (agent report with its revision, or a poll)
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    cap = Capture()
    poll.log.addHandler(cap)
    level = poll.log.level
    poll.log.setLevel(logging.INFO)   # the router runs at INFO; without its logging setup the logger would drop the line
    try:
        n = fresh_node()
        push(n)
        push(n)   # same list again: no line
        poll.apply_ollama_state(n, *openai_node.models_to_ollama(standby_v1), time.time())
    finally:
        poll.log.removeHandler(cap)
        poll.log.setLevel(level)
    models_lines = [x for x in lines if "models from" in x]
    check("journal: one line per change, naming the agent report and its revision",
          len(models_lines) == 2 and "agent report (rev 1)" in models_lines[0] and f"+{SERVER},{SMALL}" in models_lines[0],
          str(models_lines))
    check("journal: a poll that drops a model says so", len(models_lines) == 2 and "from poll" in models_lines[1]
          and f"-{SERVER}" in models_lines[1], str(models_lines))


asyncio.run(main())
print(f"\n{len(FAILS)} FAIL" if FAILS else "\nall PASS")
sys.exit(1 if FAILS else 0)
