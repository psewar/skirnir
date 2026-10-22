"""Performance data (passive + benchmark) and model profiling for the catalog."""

import asyncio
import os
import time

from aiohttp import ClientError, ClientTimeout, web

from . import config, nodes, poll, state
from .common import GIB, load_json_file, log, read_json, save_json_file
from .outcome import bad_gateway, failed, unavailable


def perf_path():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "perf.json")


def perf_load():
    data = load_json_file(perf_path(), "perf.json")
    if isinstance(data, dict):
        state.PERF.update(data)
    elif data is not None:
        log.warning("perf.json is not a mapping - ignored")


def perf_save():
    save_json_file(perf_path(), state.PERF, "perf.json", ensure_ascii=False, indent=1)


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def perf_stats(stats, ttft_s):
    """Ollama response fields (nanoseconds) -> tok/s and milliseconds. None if unusable."""
    if not isinstance(stats, dict):
        return None
    ec, ed, pc, pd, ld = (stats.get(k) or 0 for k in ("eval_count", "eval_duration", "prompt_eval_count", "prompt_eval_duration", "load_duration"))
    if not all(_num(x) for x in (ec, ed, pc, pd, ld)) or ec < 2 or ed <= 0:
        return None
    return {"gen_tps": round(ec / (ed / 1e9), 1),
            "prompt_tps": round(pc / (pd / 1e9), 1) if pc and pd else None,
            "ttft_ms": round(ttft_s * 1000) if ttft_s is not None else None,
            "load_ms": round(ld / 1e6),
            "tokens": ec, "prompt_tokens": pc}


async def _ollama(node, method, path, body=None, timeout=60, answer=True):
    """Query Ollama on the node (tunnel or direct): the JSON response as a dict or a Problem 502 with the reason.
    answer=False: only success counts, the body is read and discarded ({}) - /api/generate for loading/unloading
    streams NDJSON (multiple lines), not a single JSON document."""
    try:
        async with nodes.nreq(node, method, path, json=body, timeout=ClientTimeout(total=timeout)) as r:
            if r.status >= 400:
                return bad_gateway(f"Ollama {r.status}: {(await r.text())[:200]}")
            if not answer:
                await r.read()
                return {}
            data = await r.json(content_type=None)
    except (ClientError, asyncio.TimeoutError, ValueError) as e:  # exception-ok: network boundary and the node's JSON
        return bad_gateway(f"Ollama {path}: {e or type(e).__name__}")
    return data if isinstance(data, dict) else bad_gateway(f"Ollama {path}: not a JSON answer")


def perf_record(model, node_name, stats, ttft_s, kind="passive"):
    s = perf_stats(stats, ttft_s)
    if not s or s["load_ms"] > 1500:      # only count warm runs
        return
    key = f"{model}@{node_name}"
    e = state.PERF.setdefault(key, {"model": model, "node": node_name, "samples": [], "count": 0})
    e["count"] = e.get("count", 0) + 1
    e["last"] = {**s, "at": time.strftime("%Y-%m-%d %H:%M"), "kind": kind}
    if kind == "passive":
        node = state.NODES.get(node_name)
        if node is not None:
            node.note_perf(model, s, time.time())   # real traffic only, benchmarks would skew the HA baseline
        e["samples"] = (e.get("samples") or [])[-19:] + [s]
        smp = e["samples"]
        e["passive"] = {"gen_tps": round(sum(x["gen_tps"] for x in smp) / len(smp), 1),
                        "prompt_tps": round(sum(x["prompt_tps"] for x in smp if x["prompt_tps"]) / max(1, sum(1 for x in smp if x["prompt_tps"])), 1),
                        "ttft_ms": round(sum(x["ttft_ms"] for x in smp if x["ttft_ms"] is not None) / max(1, sum(1 for x in smp if x["ttft_ms"] is not None))),
                        "n": len(smp)}
    # Stage 3: exponentially smoothed metrics for the score (reacts faster than the 20-sample average)
    ew = e.setdefault("ewma", {})
    ew["gen_tps"] = round(s["gen_tps"] if ew.get("gen_tps") is None else EWMA_ALPHA * s["gen_tps"] + (1 - EWMA_ALPHA) * ew["gen_tps"], 1)
    if s["ttft_ms"] is not None:
        ew["ttft_ms"] = round(s["ttft_ms"] if ew.get("ttft_ms") is None else EWMA_ALPHA * s["ttft_ms"] + (1 - EWMA_ALPHA) * ew["ttft_ms"])
    state.PERF_DIRTY[0] = time.time()


EWMA_ALPHA = 0.3
OUTCOMES = ("ok", "error", "timeout", "structured_error")
RECENT_N = 20


def perf_outcome(model, node_name, outcome):
    """Stage 3: success/error/timeout/structured-output rates per model@node (all runs, including cold)."""
    if outcome not in OUTCOMES:
        return
    e = state.PERF.setdefault(f"{model}@{node_name}", {"model": model, "node": node_name, "samples": [], "count": 0})
    o = e.setdefault("outcomes", {k: 0 for k in OUTCOMES})
    o[outcome] = o.get(outcome, 0) + 1
    o["recent"] = (o.get("recent") or [])[-(RECENT_N - 1):] + [outcome]
    if outcome != "ok":
        o["last_error_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state.PERF_DIRTY[0] = time.time()


def error_rate(model, node_name):
    """Fraction of non-ok in the last RECENT_N results (0..1); 0 if no data."""
    recent = ((state.PERF.get(f"{model}@{node_name}") or {}).get("outcomes") or {}).get("recent") or []
    return (sum(1 for x in recent if x != "ok") / len(recent)) if recent else 0.0


def ewma_gen_tps(model, node_name):
    return ((state.PERF.get(f"{model}@{node_name}") or {}).get("ewma") or {}).get("gen_tps")


BENCH_PROMPT = ("Erkläre in etwa 200 Wörtern, was ein Netzwerk-Router macht, welche Aufgaben er im Heimnetz übernimmt "
                "und worin er sich von einem Switch unterscheidet. Antworte auf Deutsch in ganzen Sätzen.")


async def bench_model(model, node):
    """Standardized warm benchmark: 1 warmup + 2 measurement runs with identical prompt, num_predict 200, temperature 0."""
    state.BENCHING[model] = {"node": node.name, "step": "start", "started": time.time(), "error": None}
    node.inflight += 1
    try:
        ctx = node.loaded_context(model) or 8192
        runs = await _bench_runs(model, node, ctx)
        if failed(runs):
            log.warning("bench %s on %s failed: %s", model, node.name, runs.message)
            state.BENCHING[model].update(step="error", error=runs.message)
            return
        med = lambda k: sorted(x[k] for x in runs if x[k] is not None)[len(runs) // 2]  # noqa: E731
        key = f"{model}@{node.name}"
        e = state.PERF.setdefault(key, {"model": model, "node": node.name, "samples": [], "count": 0})
        e["bench"] = {"gen_tps": med("gen_tps"), "prompt_tps": med("prompt_tps"), "ttft_ms": med("ttft_ms"),
                      "tokens": runs[-1]["tokens"], "prompt_tokens": runs[-1]["prompt_tokens"], "num_ctx": ctx,
                      "at": time.strftime("%Y-%m-%d %H:%M"), "gpu": node.gpu}
        perf_save()
        log.info("bench %s on %s: %.1f tok/s gen, %s tok/s prompt, ttft %d ms (ctx %d)", model, node.name,
                 e["bench"]["gen_tps"], e["bench"]["prompt_tps"], e["bench"]["ttft_ms"], ctx)
        state.remember({"event": "bench", "node": node.name, "model": model, "gen_tps": e["bench"]["gen_tps"]})
        state.BENCHING[model]["step"] = "done"
    finally:
        node.inflight -= 1
        await _release_node(node, model, "bench")
        state.BENCHING.pop(model, None)


async def _bench_runs(model, node, ctx):
    """Warmup + 2 measurement runs: the two measurements or a Problem."""
    body = {"model": model, "stream": False, "keep_alive": "5m", "options": {"num_ctx": ctx, "num_predict": 200, "temperature": 0, "seed": 42},
            "messages": [{"role": "user", "content": BENCH_PROMPT}]}
    runs = []
    for i in range(3):
        state.BENCHING[model]["step"] = "warmup" if i == 0 else f"run {i}/2"
        t0 = time.time()
        data = await _ollama(node, "post", "/api/chat", body, timeout=900)
        if failed(data):
            return data
        wall = time.time() - t0
        s = perf_stats(data, None)
        if not s:
            return bad_gateway("no eval statistics in the answer")
        # TTFT-Naeherung im Non-Stream-Modus: Wandzeit - Generierzeit - Ladezeit
        s["ttft_ms"] = max(0, round((wall - (data.get("eval_duration") or 0) / 1e9 - (data.get("load_duration") or 0) / 1e9) * 1000))
        if i > 0:
            runs.append(s)
    return runs


def _free_node_for(model, node_name=None, prefer_loaded=False):
    """Free node with no active requests that has the model: largest card first (benchmark: loaded model first)."""
    cands = [n for n in state.NODES.values() if n.state == "free" and n.inflight == 0 and model in n.models]
    if node_name:
        cands = [n for n in cands if n.name == node_name]
    if not cands:
        return None
    key = (lambda n: (not n.is_loaded(model), -n.vram_total_gib, n.name)) if prefer_loaded else (lambda n: (-n.vram_total_gib, n.name))
    return sorted(cands, key=key)[0]


async def _release_node(node, model, why):
    """After measurement or benchmark: unload the measurement object (unless it is a rank-1 model), poll, prewarm rank-1 again."""
    rank1 = {r["tiers"][0]["model"] for r in state.CFG.roles.values()}
    if not any(node.same_blob(model, m) for m in rank1):
        await _ollama(node, "post", "/api/generate", {"model": model, "keep_alive": 0}, answer=False)   # best effort: the subsequent poll shows what remained loaded
    await poll.poll_node(node)
    if state.CFG.prewarm_on_free:
        state.spawn(poll.prewarm(node, 0, why))
    await asyncio.sleep(2)


async def handle_bench(request):
    b = await read_json(request)
    if not isinstance(b, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    model = b.get("model")
    if not model:
        return web.json_response({"error": "model missing"}, status=400)
    if state.BENCHING or state.MEASURING:
        return web.json_response({"error": "a measurement or benchmark is already running, please wait"}, status=409)
    node = _free_node_for(model, b.get("node"), prefer_loaded=True)
    if node is None:
        return web.json_response({"error": f"no free node without running requests has {model}"}, status=409)
    state.spawn(bench_model(model, node))
    return web.json_response({"started": True, "node": node.name, "model": model})


MEASURE_CTX_FIX = (8192, 32768)   # plus every context with which a role loads the model


def measure_ctxs(model):
    """Measurement points: the fixed contexts plus every one with which a tier loads the model - measured where it
    runs, instead of extrapolating from 32k to 196k."""
    tiers = {int(t["num_ctx"]) for r in state.CFG.roles.values() for t in r["tiers"] if t["model"] == model and not t.get("cloud")}
    return sorted(set(MEASURE_CTX_FIX) | tiers)


def fit_linear(points):
    """(ctx, gib)-Punkte -> (weights_gib, kv_gib_per_1k) als Ausgleichsgerade; kv nie negativ (Messrauschen)."""
    xs, ys = [p[0] / 1000.0 for p in points], [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    kv = max(0.0, sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var) if var > 0 else 0.0
    return max(0.1, my - kv * mx), kv


async def _ps_models(node):
    """Loaded models per /api/ps (list of dicts) or a Problem."""
    data = await _ollama(node, "get", "/api/ps", timeout=10)
    if failed(data):
        return data
    models = data.get("models") or []
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else bad_gateway("Ollama /api/ps: models is not a list")


async def _ps_entry(node, model):
    """Model entry in /api/ps, None if not loaded, or a Problem."""
    models = await _ps_models(node)
    return models if failed(models) else next((m for m in models if _is_model(node, m, model)), None)


async def _load(node, model, ctx):
    node.announce_load(model, ctx)
    loaded = await _ollama(node, "post", "/api/generate", {"model": model, "keep_alive": "2m", "options": {"num_ctx": ctx}}, timeout=900, answer=False)
    entry = loaded if failed(loaded) else await _ps_entry(node, model)
    if entry is None:
        return bad_gateway(f"model not in /api/ps after loading (ctx {ctx})")
    return entry


async def _unload_all(node):
    """Unload everything and wait until /api/ps is empty - otherwise the delta measures a different model. Returns what
    /api/ps previously reported as size_vram (GiB): nvidia-smi must free at least this much afterwards. Or a Problem."""
    loaded = await _ps_models(node)
    if failed(loaded):
        return loaded
    for m in loaded:
        unloaded = await _ollama(node, "post", "/api/generate", {"model": m.get("name"), "keep_alive": 0}, answer=False)
        if failed(unloaded):
            return unloaded
    for _ in range(60):
        still = await _ps_models(node)
        if failed(still):
            return still
        if not still:
            return sum(m.get("size_vram", 0) for m in loaded) / GIB
        await asyncio.sleep(1)
    return unavailable("Ollama does not unload (models still in /api/ps after 60 s)")


async def _empty_used(node, before, unloaded, timeout=120):
    """Baseline after unloading. /api/ps is empty immediately, but memory is freed seconds later - two consecutive
    heartbeats proved this not to be the case (0.3.5: after unloading 26 GiB, the measurement read "empty" while the
    card was still full, the first measurement point was negative). Therefore wait until nvidia-smi has freed at least 90 % of what
    /api/ps previously reported, then read the idle usage. Or a Problem."""
    if before is not None and unloaded > 0.5:
        target = before - 0.9 * unloaded
        deadline = time.time() + timeout
        while node.vram_used_gib is None or node.vram_used_gib > target:
            if time.time() > deadline:
                return unavailable(f"memory is not freed after unloading ({node.vram_used_gib} GiB, expected <= {target:.2f})")
            await asyncio.sleep(0.5)
    await asyncio.sleep(state.CFG.vram_settle_s)
    return await _stable_used(node, time.time())


async def _stable_used(node, after, timeout=90):
    """Usage per nvidia-smi (Heartbeat), once stable: two heartbeats after `after`, < 0.1 GiB apart.
    Or a Problem if it does not settle within `timeout`."""
    last, last_ts = None, after
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        if node.hb_ts and node.hb_ts > last_ts and node.vram_used_gib is not None:
            if last is not None and abs(node.vram_used_gib - last) < 0.1:
                return node.vram_used_gib
            last, last_ts = node.vram_used_gib, node.hb_ts
    return unavailable("VRAM use did not settle (heartbeats)")


async def _measure_real(model, node):
    """Since 0.3.5: for each context unload everything, read baseline, load, delta per nvidia-smi = actual usage.
    /api/ps reports only part (qwen3.6:35b-a3b 21.0 instead of 26.4 GiB at 196k, laguna-xs-2.1 19.2 instead of 29.7 GiB at
    131k - measured 2026-09-30); with these numbers the router miscalculated budget, foreign VRAM and baseline.
    Returns {ctx: (used, ps_size_vram, ps_size)} or a Problem."""
    res = {}
    for ctx in measure_ctxs(model):
        for attempt in range(1, MEASURE_TRIES + 1):
            point = await _measure_point(model, node, ctx)
            if failed(point):
                return point
            if isinstance(point, tuple):
                res[ctx] = point
                break
            log.info("measure %s@%d on %s: attempt %d dropped (%s)", model, ctx, node.name, attempt, point)
        else:
            return unavailable(f"measurement @{ctx} not clean after {MEASURE_TRIES} attempts: {point}")
    return res


MEASURE_TRIES = 3


def _is_model(node, m, model):
    want = node.digest_of.get(model)
    return m.get("name") == model or (want is not None and m.get("digest") == want)


async def _others_loading(node, model, timeout=300):
    """Wait until no OTHER load operation is running (prewarm, real request). 0.3.5: the prewarm after the previous measurement
    loaded qwen3.6 exactly when the next measurement read its baseline - /api/ps was still empty, nothing was unloaded.
    None once nothing else is loading, or a Problem."""
    deadline = time.time() + timeout
    while any(m != model and deadline_m > time.time() for m, (_g, deadline_m) in node.loading.items()):
        if time.time() > deadline:
            return unavailable("another load does not finish")
        await asyncio.sleep(1)
    return None


async def _measure_point(model, node, ctx):
    """A measurement point. Returns (used, ps_vram, ps_size) or a reason why it was discarded (then retry):
    another model interfered, or the delta is smaller than Ollama's own report. A Problem aborts the
    measurement."""
    waited = await _others_loading(node, model)
    if failed(waited):
        return waited
    state.MEASURING[model]["step"] = f"unloading for @{ctx}"
    before_gib = node.vram_used_gib
    unloaded = await _unload_all(node)
    empty_gib = unloaded if failed(unloaded) else await _empty_used(node, before_gib, unloaded)
    if failed(empty_gib):
        return empty_gib
    state.MEASURING[model]["step"] = f"loading @{ctx}"
    entry = await _load(node, model, ctx)
    full_gib = entry if failed(entry) else await _stable_used(node, time.time())
    loaded = full_gib if failed(full_gib) else await _ps_models(node)
    if failed(loaded):
        return loaded
    foreign = [m.get("name") for m in loaded if not _is_model(node, m, model)]
    if foreign or any(m != model for m in node.loading):
        return f"another model loaded: {foreign or list(node.loading)}"
    used_gib, sv = full_gib - empty_gib, entry.get("size_vram", 0) / GIB
    # Plausible: never less than what Ollama itself reports - otherwise the baseline was wrong, and the catalog
    # would get nonsense (0.3.5: gemma4:26b "-7.67 GiB" at 8k). Better no measurement than a wrong one.
    if used_gib < sv - 0.3:
        return f"implausible: nvidia-smi increase {used_gib:.2f} GiB < /api/ps {sv:.2f} GiB"
    return used_gib, sv, entry.get("size", 0) / GIB


async def _measure_ps(model, node):
    """Fallback without GPU values in the Heartbeat (only way until 0.3.4): /api/ps at 8k and 32k."""
    res = {}
    for ctx in MEASURE_CTX_FIX:
        state.MEASURING[model]["step"] = f"loading @{ctx}"
        entry = await _load(node, model, ctx)
        if failed(entry):
            return entry
        res[ctx] =(entry.get("size_vram", 0) / GIB, entry.get("size_vram", 0) / GIB, entry.get("size", 0) / GIB)
    return res


def catalog_entry(res, node_name, real):
    """Measurement result {ctx: (used, ps_vram, ps_size)} -> catalog entry. With partial offloading to RAM,
    nvidia-smi only measures the GPU part; the offloaded remainder (size - size_vram) is added so the catalog knows the total
    requirement and the router no longer considers the model suitable for this context (0.3.5 skipped such points:
    granite4.2:30b then showed flat 21 GiB in the catalog, although it does not fit on the card at 32k)."""
    partial = sorted(ctx for ctx, (_b, sv, st) in res.items() if sv + 0.05 < st)
    points = [(ctx, b + (max(0.0, st - sv) if ctx in partial else 0.0)) for ctx, (b, sv, st) in res.items()]
    weights, kv = fit_linear(points)
    e = {"weights_gib": round(weights, 2), "kv_gib_per_1k": round(kv, 4),
         "source": "measured (nvidia-smi)" if real else "measured", "measured_at": time.strftime("%Y-%m-%d %H:%M"),
         "measured_on": node_name}
    if real:
        e.update(real=True, vram_real_gib={str(c): round(v[0], 2) for c, v in sorted(res.items())},
                 vram_ps_gib={str(c): round(v[1], 2) for c, v in sorted(res.items())})
    else:
        e.update(vram_gib_8k=round(res[8192][1], 2), vram_gib_32k=round(res[32768][1], 2))
    if partial:
        e["partial_offload"] = partial
    return e


async def measure_model(model, node):
    """Profile the model and write the catalog entry to roles.yaml. With GPU values in the Heartbeat (Agent) real per
    nvidia-smi for each used context, otherwise like until 0.3.4 per /api/ps. Runs only on a free node without
    active requests; unloads all models during this (prewarm afterwards), the state machine rests meanwhile (poll.evaluate)."""
    state.MEASURING[model] = {"node": node.name, "step": "start", "started": time.time(), "error": None}
    node.inflight += 1          # Router does not select the node for other tasks
    try:
        real = bool(node.gpu_known(time.time()) and node.vram_used_gib is not None)
        res = await (_measure_real(model, node) if real else _measure_ps(model, node))
        if failed(res):
            log.warning("measure %s on %s failed: %s", model, node.name, res.message)
            state.MEASURING[model].update(step="error", error=res.message)
            return
        e = catalog_entry(res, node.name, real)
        ov = config.read_overrides()
        models = dict(ov.get("models", {}))
        models[model] = e
        ov["models"] = models
        written = config.write_overrides(ov)
        if failed(written):   # Measurement succeeded, catalog not written: report as error, not as done
            log.warning("measure %s on %s: catalog not written: %s", model, node.name, written.message)
            state.MEASURING[model].update(step="error", error=written.message)
            return
        partial = f", PARTIAL OFFLOAD bei {e['partial_offload']}" if e.get("partial_offload") else ""
        log.info("measured %s on %s (%s): weights %.2f GiB, kv %.4f GiB/1k, points %s%s", model, node.name,
                 "nvidia-smi" if real else "/api/ps", e["weights_gib"], e["kv_gib_per_1k"],
                 {c: round(v[0], 2) for c, v in sorted(res.items())}, partial)
        state.remember({"event": "measure", "node": node.name, "model": model, "weights_gib": e["weights_gib"],
                        "kv_gib_per_1k": e["kv_gib_per_1k"], "real": real})
        state.MEASURING[model]["step"] = "done"
    finally:
        node.inflight -= 1
        node.finish_load(model)
        await _release_node(node, model, "measure")
        state.MEASURING.pop(model, None)


async def handle_measure(request):
    b = await read_json(request)
    if not isinstance(b, dict):
        return web.json_response({"error": "invalid json"}, status=400)
    model = b.get("model")
    if not model:
        return web.json_response({"error": "model missing"}, status=400)
    if model in state.MEASURING:
        return web.json_response({"error": f"{model} is being measured right now"}, status=409)
    if len(state.MEASURING) >= 1:
        return web.json_response({"error": "a measurement is already running, please wait"}, status=409)
    node = _free_node_for(model, b.get("node"))
    if node is None:
        return web.json_response({"error": f"no free node without running requests has {model}"}, status=409)
    state.spawn(measure_model(model, node))
    return web.json_response({"started": True, "node": node.name, "model": model})
