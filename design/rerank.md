# Optional rerank endpoint (`POST /rerank`)

The router can pass reranking requests to a reranker service on a GPU node. The intended caller is a memory service
whose own reranker runs on CPU (for example Hindsight with its TEI provider): with a node available the reranking runs on
the node's accelerator, without one the caller falls back to its own CPU reranker at once.

Disabled by default (`router.rerank.enabled: false` answers 404). Nothing else in the router changes when it is off.

## Path of a request

```
caller --POST /rerank (11434)--> router --tunnel stream--> agent route "rerank" --> reranker service (loopback, child of the agent)
```

The agent forwards only the paths listed in its `tunnel.routes`; everything else still goes to Ollama. It checks the
reranker's health endpoint itself and reports the result in every heartbeat:

```json
"routes": {"rerank": {"up": true, "reason": ""}}
```

`reason` is empty or one of `unreachable`, `unhealthy`, `starting`, `selftest_failed`. An agent that does not send the field
(older version) counts as having no reranker.

## Wire format (TEI)

- Request: `{"query": str, "texts": [str], "return_text": false}`. Other TEI fields (`raw_scores`, `truncate`) are ignored;
  only `query` and `texts` reach the node.
- Answer `200`: `[{"index": int, "score": float}, ...]`, exactly one entry per text, sorted by score descending
  (with `return_text: true` each entry also carries its `text`). Scores are as TEI reports them (sigmoid, 0..1).
- The router checks the node's answer before passing it on: a list with one entry per text, every `index` an integer in
  range and unique, every `score` a finite number.

## Configuration

```yaml
router:
  rerank:
    enabled: false
    nodes: [gpu-node-a]       # order of preference; the first usable node serves
    timeout_s: 2.0            # whole exchange with the node, tunnel included
    busy_ok: true             # a busy node (game, foreign VRAM) may still rerank, e.g. on an integrated GPU
    max_texts: 512            # more -> 413
    max_body_bytes: 4194304   # larger body -> 413
  client_auth:
    clients:
      memory-service:
        ip: ["192.0.2.40"]    # TEI clients send no header: fixed source address of the caller
        rerank: true          # the right to call POST /rerank; default false
```

`rerank` is a right of its own. A client without it, or an unidentified request in `observe` mode, gets `403`.

## Choosing a node

The first node in `rerank.nodes` that is known to the router, has a fresh heartbeat, has a connected tunnel, reports
`routes.rerank.up == true` and, unless `busy_ok`, is not busy. Whether Ollama itself answers does not matter: the reranker is
a separate service. There is no Wake-on-LAN, no queue and no retry; a caller with a fallback is faster served by that.

## Failures: 424, not 503

When no reranker is usable the router answers immediately with

```json
424 {"error": {"code": "rerank_unavailable", "reason": "<reason>"}}
```

and no `Retry-After`. Reasons: `disabled_node` (not configured, not approved or revoked), `node_offline` (no fresh
heartbeat), `tunnel_down`, `reranker_down`, `gpu_busy`, `timeout`, `upstream_invalid` (answer failed the check),
`upstream_error` (non-200 or broken tunnel). With several nodes the reason is that of the first, preferred node.

Why 424: Hindsight's TEI client retries 429 and every 5xx three times with backoff (honouring `Retry-After`) and has no
circuit breaker. Every recall would then wait seconds before its CPU fallback. Other 4xx are not retried; the client moves to
the next reranker of its chain at once.

Invalid input is answered with `400` (`code: invalid`) or `413` (`code: too_large`) before any node is asked.

## Metrics

- `skirnir_rerank_requests_total{outcome}`: `ok`, one of the reasons above, or `disabled`, `forbidden`, `invalid`, `too_large`.
- `skirnir_rerank_duration_seconds{outcome}`: histogram of the time spent in the router, node exchange included.

## Rollback

Set `router.rerank.enabled: false` (reload with SIGHUP) or remove the client right. The caller keeps working with its own
reranker. The reranker stores nothing.
