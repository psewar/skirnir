"""OpenAI-compatible nodes: a node whose agent fronts an inference server with the OpenAI API instead of Ollama
(agent `tunnel.upstream_api: openai`, agent fact `upstream_api: "openai"`), e.g. Strata.

What the router does differently for such a node:
- Models come from /v1/models. The agent reports them in the OLLAMA frame as Ollama-shaped entries (agent
  ollamawatch.go openAIEntries); `models_to_ollama` is the same conversion for the router's own poll (poll.poll_node).
  Sizes stay 0: the server's GPU memory is counted once, as the agent's child (children_vram_mib).
- Chat and generate requests go to /v1/chat/completions through the tunnel, translated by the cloud adapter's OpenAI path
  (cloud.openai_exchange) - the answer reaches every client format (Ollama, /v1, Anthropic) as Ollama chunks, like a
  cloud answer does.
- No Ollama-only calls: no /api/show (capabilities come from the catalog: models.<name>.capabilities), no prewarm and no
  busy unload (the server holds its model for as long as it runs; a busy node is simply not chosen), no /api/version.
- Not in this first version: embeddings and managed session turns. Managed turns persist native Ollama chunks and stay on
  Ollama nodes, like they stay off cloud tiers (managed.ManagedTurn.target).
"""

from aiohttp import ClientTimeout

from . import cloud, nodes, state

API = "openai"
PATHS = ("/api/chat", "/api/generate")   # what an OpenAI-compatible node serves; embeddings stay on Ollama nodes
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"


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
    return await cloud.openai_exchange(post, path, body, model, model, stream, max_tokens_field=True)
