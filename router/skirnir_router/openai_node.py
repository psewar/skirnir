"""OpenAI-compatible nodes: a node whose agent fronts an inference server with the OpenAI API instead of Ollama
(agent `tunnel.upstream_api: openai`, agent fact `upstream_api: "openai"`), e.g. Strata.

What the router does differently for such a node:
- Models come from /v1/models. The agent reports them in the OLLAMA frame as Ollama-shaped entries (agent
  ollamawatch.go openAIEntries); `models_to_ollama` is the same conversion for the router's own poll (poll.poll_node).
  Sizes stay 0: the server's GPU memory is counted once, as the agent's child (children_vram_mib).
- Chat and generate requests go to /v1/chat/completions through the tunnel, translated by the cloud adapter's OpenAI path
  (cloud.openai_exchange) - the answer reaches every client format (Ollama, /v1, Anthropic) as Ollama chunks, like a
  cloud answer does.
- No Ollama-only calls: no /api/show (capabilities come from the catalog: models.<name>.capabilities), no /api/version,
  no preloading or unloading through /api/generate.
- Freeing the GPU (router 0.11.0, agent 0.22.0, agent fact upstream_release): the server cannot unload its model, so
  when the node turns busy (a game) the router orders the agent to stop it ("hold"), and when the node is free again
  (where Ollama nodes get their prewarm) or a request needs the model, it orders a start ("release"). The agent reports
  the server's state as a fact (OLLAMA frame upstream_state: held / starting). Without the fact a busy node keeps its
  model and is merely skipped.
- Not in this version: embeddings and managed session turns. Managed turns persist native Ollama chunks and stay on
  Ollama nodes, like they stay off cloud tiers (managed.ManagedTurn.target).
"""

import asyncio
import time

from aiohttp import ClientTimeout

from . import cloud, nodes, state
from .common import log

API = "openai"
PATHS = ("/api/chat", "/api/generate")   # what an OpenAI-compatible node serves; embeddings stay on Ollama nodes
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
HELD, STARTING = "held", "starting"      # agent report OLLAMA.upstream_state
START_WAIT_S = 180   # how long a request waits for a released server; a large model loads in ~25-60 s (Strata, 2026-10-23)


async def order(node, what, reason):
    """Send "hold" or "release" to the node's agent. False if this node cannot (no tunnel, agent without the fact
    upstream_release). The agent ignores a repeated order, so sending again while its report lags is harmless."""
    if not node.upstream_releasable or node.tunnel is None:
        return False
    from .registry import send_ctl   # registry imports poll, which imports this module
    await send_ctl(node.tunnel, {"t": f"upstream-{what}"})
    node.upstream_order = what
    log.info("node %s: upstream-%s (%s)", node.name, what, reason)
    state.remember({"event": f"upstream_{what}", "node": node.name, "reason": reason})
    return True


def holdable(node):
    """Loaded models the busy node should give up: everything except busy_ok models (those may stay during a game)."""
    return [m for m in node.loaded if not any(node.same_blob(m, ok) for ok in state.CFG.busy_ok_models)]


async def ensure_loaded(node, model, timeout_s=START_WAIT_S):
    """A request needs the model of a held or starting server: release it if needed and wait until the agent reports the
    model loaded - the way Ollama loads a model on the first request. False = not loaded in time (next candidate)."""
    if node.is_loaded(model):
        return True
    if node.upstream_state != STARTING and not await order(node, "release", "request"):
        return False   # cannot be started by the router: nothing to wait for
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if node.is_loaded(model):
            return True
        if node.state == "offline":
            return False
        await asyncio.sleep(1)
    log.warning("node %s: %s not loaded %d s after release", node.name, model, timeout_s)
    return False


def models_to_ollama(data):
    """/v1/models -> (tags, ps) in the shape poll.apply_ollama_state reads. Mirrors the agent's openAIEntries: a model with
    a status other than "loaded" (Strata's extra field) is installed, not loaded; meta.n_ctx becomes context_length."""
    tags, ps = [], []
    entries = data.get("data") if isinstance(data, dict) else None
    for m in entries if isinstance(entries, list) else []:
        mid = m.get("id") if isinstance(m, dict) else None
        if not isinstance(mid, str) or not mid:
            continue
        digest = f"openai:{mid}"
        tags.append({"name": mid, "model": mid, "size": 0, "digest": digest, "details": {"format": "openai"}})
        status = m.get("status")
        if isinstance(status, dict) and status.get("value") != "loaded":
            continue
        entry = {"name": mid, "model": mid, "size": 0, "size_vram": 0, "digest": digest}
        meta = m.get("meta")
        n_ctx = meta.get("n_ctx") if isinstance(meta, dict) else None
        if isinstance(n_ctx, int) and not isinstance(n_ctx, bool) and n_ctx > 0:
            entry["context_length"] = n_ctx
        ps.append(entry)
    return tags, ps


async def run(node, path, body, model, stream):
    """One request to the node's server. Returns (info, chunks) like cloud.run; HTTP errors raise cloud.CloudError.
    The model name goes out as it is: the server knows it under the name it reported in /v1/models."""
    timeout = ClientTimeout(total=state.CFG.request_timeout_s, sock_read=state.CFG.request_timeout_s)

    def post(req):
        return nodes.nreq(node, "post", CHAT_PATH, json=req, timeout=timeout)
    return await cloud.openai_exchange(post, path, body, model, model, stream, local_server=True)
