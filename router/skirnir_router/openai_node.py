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
- Embeddings go to /v1/embeddings (cloud.openai_embed). A server without that endpoint answers 404; the request then
  takes the next candidate and the node keeps its chat model (Strata 0.1.46 has no embeddings).
- Managed session turns are served too (router 0.11.0): the translated chunks are native Ollama chunks, so the proxy
  captures and commits them as on an Ollama node. Cloud tiers stay excluded from managed turns (data class, not technique).
- Standby Ollama (router 0.12.0, agent 0.23.0, agent fact upstream_fallback): while the server is held, the agent can run
  an Ollama in its place. The OLLAMA frame then says serving "ollama" and lists Ollama's models plus the held server's
  (digest "openai:<id>"). The node is an Ollama node meanwhile (`speaks_openai` false: poll, unload and busy_ok models as
  usual); only a request for the server's model goes the OpenAI way (`serves_openai`) and releases it, and prewarm
  releases it instead of loading anything into the standby. The agent stops the standby before the server starts.
"""

import asyncio
import time

from aiohttp import ClientTimeout

from . import cloud, nodes, state
from .common import log

API = "openai"
PATHS = ("/api/chat", "/api/generate", *cloud.EMBED_PATHS)   # what an OpenAI-compatible node serves
CHAT_PATH = "/v1/chat/completions"
EMBED_PATH = "/v1/embeddings"
MODELS_PATH = "/v1/models"
HELD, STARTING = "held", "starting"      # agent report OLLAMA.upstream_state
SERVING_OLLAMA = nodes.openai_node_serving_ollama   # agent report OLLAMA.serving: the standby Ollama stands in (agent 0.23.0)
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


def server_allowed(node, now):
    """Router 0.16.0: may the node's OpenAI-compatible server run on its own (prewarm, release after a game, the night
    window)? Always without modes.upstream_window; with it only inside the window. A request for the server's model starts
    it at any time (ensure_loaded), and upstream_window_check holds it again once it is idle outside the window."""
    w = state.CFG.upstream_window
    if not w["enabled"] or not node.can_switch_to_standby:
        return True
    from .ollamaupdate import in_window   # same window semantics as the Ollama update (HH:MM, across midnight, timezone)
    return in_window(w, now)


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


async def ensure_standby(node, model, timeout_s=START_WAIT_S):
    """A request needs a model only the node's standby Ollama has (router 0.15.0, node.standby_switch): hold the server
    and wait until the agent reports the standby serving with the model listed. The way back is the existing one: a
    request for the server's model, or prewarm once the node is idle or free again, releases the server. False = not
    serving in time or the hold could not be sent (next candidate)."""
    if not await order(node, "hold", f"request {model}"):
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if node.upstream_serving == SERVING_OLLAMA and model in node.models:
            return True
        if node.state == "offline":
            return False
        await asyncio.sleep(1)
    log.warning("node %s: standby Ollama not serving %s %d s after the hold", node.name, model, timeout_s)
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
    target = EMBED_PATH if path in cloud.EMBED_PATHS else CHAT_PATH

    def post(req):
        return nodes.nreq(node, "post", target, json=req, timeout=timeout)
    if target == EMBED_PATH:
        return await cloud.openai_embed(post, body, model, model)
    return await cloud.openai_exchange(post, path, body, model, model, stream, local_server=True)
