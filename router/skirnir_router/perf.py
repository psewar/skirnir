"""Leistungsdaten (passiv + Benchmark) und das Vermessen von Modellen fuer den Katalog."""

import asyncio
import json
import os
import time

from aiohttp import ClientTimeout, web

from . import config, nodes, poll, state
from .common import GIB, log


def perf_path():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "perf.json")


def perf_load():
    try:
        with open(perf_path(), encoding="utf-8") as f:
            state.PERF.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("perf.json unlesbar: %s", e)


def perf_save():
    try:
        tmp = perf_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.PERF, f, ensure_ascii=False, indent=1)
        os.replace(tmp, perf_path())
    except Exception as e:  # noqa: BLE001
        log.warning("perf.json schreiben: %s", e)


def perf_stats(stats, ttft_s):
    """Ollama-Antwortfelder (Nanosekunden) -> tok/s und Millisekunden. None wenn unbrauchbar."""
    try:
        ec, ed = stats.get("eval_count") or 0, stats.get("eval_duration") or 0
        pc, pd = stats.get("prompt_eval_count") or 0, stats.get("prompt_eval_duration") or 0
        if ec < 2 or ed <= 0:
            return None
        return {"gen_tps": round(ec / (ed / 1e9), 1),
                "prompt_tps": round(pc / (pd / 1e9), 1) if pc and pd else None,
                "ttft_ms": round(ttft_s * 1000) if ttft_s is not None else None,
                "load_ms": round((stats.get("load_duration") or 0) / 1e6),
                "tokens": ec, "prompt_tokens": pc}
    except Exception:  # noqa: BLE001
        return None


def perf_record(model, node_name, stats, ttft_s, kind="passive"):
    s = perf_stats(stats, ttft_s)
    if not s or s["load_ms"] > 1500:      # nur warme Laeufe zaehlen
        return
    key = f"{model}@{node_name}"
    e = state.PERF.setdefault(key, {"model": model, "node": node_name, "samples": [], "count": 0})
    e["count"] = e.get("count", 0) + 1
    e["last"] = {**s, "at": time.strftime("%Y-%m-%d %H:%M"), "kind": kind}
    if kind == "passive":
        e["samples"] = (e.get("samples") or [])[-19:] + [s]
        smp = e["samples"]
        e["passive"] = {"gen_tps": round(sum(x["gen_tps"] for x in smp) / len(smp), 1),
                        "prompt_tps": round(sum(x["prompt_tps"] for x in smp if x["prompt_tps"]) / max(1, sum(1 for x in smp if x["prompt_tps"])), 1),
                        "ttft_ms": round(sum(x["ttft_ms"] for x in smp if x["ttft_ms"] is not None) / max(1, sum(1 for x in smp if x["ttft_ms"] is not None))),
                        "n": len(smp)}
    # Stufe 3: exponentiell geglaettete Kennzahlen fuer den Score (reagieren schneller als der 20er-Mittelwert)
    ew = e.setdefault("ewma", {})
    ew["gen_tps"] = round(s["gen_tps"] if ew.get("gen_tps") is None else EWMA_ALPHA * s["gen_tps"] + (1 - EWMA_ALPHA) * ew["gen_tps"], 1)
    if s["ttft_ms"] is not None:
        ew["ttft_ms"] = round(s["ttft_ms"] if ew.get("ttft_ms") is None else EWMA_ALPHA * s["ttft_ms"] + (1 - EWMA_ALPHA) * ew["ttft_ms"])
    state.PERF_DIRTY[0] = time.time()


EWMA_ALPHA = 0.3
OUTCOMES = ("ok", "error", "timeout", "structured_error")
RECENT_N = 20


def perf_outcome(model, node_name, outcome):
    """Stufe 3: Erfolgs-/Fehler-/Timeout-/Structured-Output-Raten je Modell@Knoten (alle Laeufe, auch kalte)."""
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
    """Anteil Nicht-ok der letzten RECENT_N Ergebnisse (0..1); 0 ohne Daten."""
    recent = ((state.PERF.get(f"{model}@{node_name}") or {}).get("outcomes") or {}).get("recent") or []
    return (sum(1 for x in recent if x != "ok") / len(recent)) if recent else 0.0


def ewma_gen_tps(model, node_name):
    return ((state.PERF.get(f"{model}@{node_name}") or {}).get("ewma") or {}).get("gen_tps")


BENCH_PROMPT = ("Erkläre in etwa 200 Wörtern, was ein Netzwerk-Router macht, welche Aufgaben er im Heimnetz übernimmt "
                "und worin er sich von einem Switch unterscheidet. Antworte auf Deutsch in ganzen Sätzen.")


async def bench_model(model, node):
    """Standardisierter Warm-Benchmark: 1 Aufwaermlauf + 2 Messlaeufe mit identischem Prompt, num_predict 200, temperature 0."""
    state.BENCHING[model] = {"node": node.name, "step": "start", "started": time.time(), "error": None}
    node.inflight += 1
    try:
        ctx = node.loaded_context(model) or 8192
        body = {"model": model, "stream": False, "keep_alive": "5m", "options": {"num_ctx": ctx, "num_predict": 200, "temperature": 0, "seed": 42},
                "messages": [{"role": "user", "content": BENCH_PROMPT}]}
        runs = []
        for i in range(3):
            state.BENCHING[model]["step"] = "aufwärmen" if i == 0 else f"Messlauf {i}/2"
            t0 = time.time()
            async with nodes.nreq(node, "post", "/api/chat", json=body, timeout=ClientTimeout(total=900)) as r:
                if r.status >= 400:
                    raise RuntimeError(f"Ollama {r.status}: {(await r.text())[:200]}")
                data = await r.json(content_type=None)
            wall = time.time() - t0
            s = perf_stats(data, None)
            if not s:
                raise RuntimeError("keine eval-Statistik in der Antwort")
            # TTFT-Naeherung im Non-Stream-Modus: Wandzeit - Generierzeit - Ladezeit
            s["ttft_ms"] = max(0, round((wall - (data.get("eval_duration") or 0) / 1e9 - (data.get("load_duration") or 0) / 1e9) * 1000))
            if i > 0:
                runs.append(s)
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
        state.BENCHING[model]["step"] = "fertig"
    except Exception as ex:  # noqa: BLE001
        log.warning("bench %s on %s failed: %s", model, node.name, ex)
        state.BENCHING[model].update(step="fehler", error=str(ex))
    finally:
        node.inflight -= 1
        await _release_node(node, model, "bench")
        state.BENCHING.pop(model, None)


def _free_node_for(model, node_name=None, prefer_loaded=False):
    """Freier Knoten ohne laufende Anfragen, der das Modell hat: groesste Karte zuerst (Benchmark: geladenes Modell zuerst)."""
    cands = [n for n in state.NODES.values() if n.state == "free" and n.inflight == 0 and model in n.models]
    if node_name:
        cands = [n for n in cands if n.name == node_name]
    if not cands:
        return None
    key = (lambda n: (not n.is_loaded(model), -n.vram_total_gib, n.name)) if prefer_loaded else (lambda n: (-n.vram_total_gib, n.name))
    return sorted(cands, key=key)[0]


async def _release_node(node, model, why):
    """Nach Messung oder Benchmark: Messobjekt entladen (ausser es ist ein Rang-1-Modell), Poll, Rang-1 wieder vorwaermen."""
    rank1 = {r["tiers"][0]["model"] for r in state.CFG.roles.values()}
    if not any(node.same_blob(model, m) for m in rank1):
        try:
            async with nodes.nreq(node, "post", "/api/generate", json={"model": model, "keep_alive": 0}, timeout=ClientTimeout(total=60)) as r:
                await r.read()
        except Exception:  # noqa: BLE001
            pass
    await poll.poll_node(node)
    if state.CFG.prewarm_on_free:
        state.spawn(poll.prewarm(node, 0, why))
    await asyncio.sleep(2)


async def handle_bench(request):
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
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


MEASURE_CTX_FIX = (8192, 32768)   # dazu jeder Kontext, mit dem eine Rolle das Modell laedt


def measure_ctxs(model):
    """Messpunkte: die festen Kontexte plus jeder, mit dem eine Stufe das Modell laedt - gemessen wird dort, wo es
    laeuft, statt von 32k auf 196k hochzurechnen."""
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
    async with nodes.nreq(node, "get", "/api/ps", timeout=ClientTimeout(total=10)) as r:
        return (await r.json()).get("models", [])


async def _ps_entry(node, model):
    want = node.digest_of.get(model)
    return next((m for m in await _ps_models(node) if m.get("name") == model or (want and m.get("digest") == want)), None)


async def _load(node, model, ctx):
    node.announce_load(model, ctx)
    body = {"model": model, "keep_alive": "2m", "options": {"num_ctx": ctx}}
    async with nodes.nreq(node, "post", "/api/generate", json=body, timeout=ClientTimeout(total=900)) as r:
        if r.status >= 400:
            raise RuntimeError(f"Ollama {r.status}: {(await r.text())[:200]}")
    entry = await _ps_entry(node, model)
    if not entry:
        raise RuntimeError(f"Modell nach dem Laden nicht in /api/ps (ctx {ctx})")
    return entry


async def _unload_all(node):
    """Alles entladen und warten, bis /api/ps leer ist - sonst misst der Zuwachs ein anderes Modell mit."""
    for m in await _ps_models(node):
        async with nodes.nreq(node, "post", "/api/generate", json={"model": m.get("name"), "keep_alive": 0},
                              timeout=ClientTimeout(total=60)) as r:
            await r.read()
    for _ in range(60):
        if not await _ps_models(node):
            return
        await asyncio.sleep(1)
    raise RuntimeError("Ollama entlaedt nicht (nach 60 s noch Modelle in /api/ps)")


async def _stable_used(node, after, timeout=90):
    """Belegung laut nvidia-smi (Heartbeat), sobald sie steht: zwei Heartbeats nach `after`, < 0,1 GiB auseinander."""
    last, last_ts = None, after
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        if node.hb_ts and node.hb_ts > last_ts and node.vram_used_gib is not None:
            if last is not None and abs(node.vram_used_gib - last) < 0.1:
                return node.vram_used_gib
            last, last_ts = node.vram_used_gib, node.hb_ts
    raise RuntimeError("VRAM-Belegung kam nicht zur Ruhe (Heartbeats)")


async def _measure_real(model, node):
    """Seit 0.3.5: je Kontext alles entladen, Grundlast ablesen, laden, Zuwachs laut nvidia-smi = echte Belegung.
    /api/ps meldet nur einen Teil (qwen3.6:35b-a3b 21,0 statt 26,4 GiB bei 196k, laguna-xs-2.1 19,2 statt 29,7 GiB bei
    131k - gemessen 2026-09-30); mit diesen Zahlen verrechnete sich der Router bei Budget, fremdem VRAM und Grundlast.
    Liefert {ctx: (belegt, ps_size_vram, ps_size)}."""
    res = {}
    for ctx in measure_ctxs(model):
        state.MEASURING[model]["step"] = f"entlade fuer @{ctx}"
        await _unload_all(node)
        await asyncio.sleep(state.CFG.vram_settle_s)
        leer = await _stable_used(node, time.time())
        state.MEASURING[model]["step"] = f"lade @{ctx}"
        entry = await _load(node, model, ctx)
        voll = await _stable_used(node, time.time())
        res[ctx] = (voll - leer, entry.get("size_vram", 0) / GIB, entry.get("size", 0) / GIB)
    return res


async def _measure_ps(model, node):
    """Rueckfall ohne GPU-Werte im Heartbeat (bis 0.3.4 der einzige Weg): /api/ps bei 8k und 32k."""
    res = {}
    for ctx in MEASURE_CTX_FIX:
        state.MEASURING[model]["step"] = f"lade @{ctx}"
        entry = await _load(node, model, ctx)
        res[ctx] = (entry.get("size_vram", 0) / GIB, entry.get("size_vram", 0) / GIB, entry.get("size", 0) / GIB)
    return res


def catalog_entry(res, node_name, real):
    """Messergebnis {ctx: (belegt, ps_vram, ps_size)} -> Katalog-Eintrag. Punkte mit Teil-Auslagerung in den RAM zaehlen
    nicht fuer die Gerade (dort misst nvidia-smi zu wenig), bleiben aber als partial_offload sichtbar."""
    partial = sorted(ctx for ctx, (_b, sv, st) in res.items() if sv + 0.05 < st)
    points = [(ctx, v[0]) for ctx, v in res.items() if ctx not in partial] or [(ctx, v[0]) for ctx, v in res.items()]
    weights, kv = fit_linear(points)
    e = {"weights_gib": round(weights, 2), "kv_gib_per_1k": round(kv, 4),
         "source": "gemessen (nvidia-smi)" if real else "gemessen", "measured_at": time.strftime("%Y-%m-%d %H:%M"),
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
    """Modell vermessen und den Katalog-Eintrag in roles.yaml schreiben. Mit GPU-Werten im Heartbeat (Agent) echt per
    nvidia-smi bei jedem genutzten Kontext, sonst wie bis 0.3.4 per /api/ps. Laeuft nur auf einem freien Knoten ohne
    laufende Anfragen; entlaedt dabei alle Modelle (danach prewarm), der Zustandsautomat ruht so lange (poll.evaluate)."""
    state.MEASURING[model] = {"node": node.name, "step": "start", "started": time.time(), "error": None}
    node.inflight += 1          # Router waehlt den Knoten nicht fuer anderes
    try:
        real = bool(node.gpu_known(time.time()) and node.vram_used_gib is not None)
        res = await (_measure_real(model, node) if real else _measure_ps(model, node))
        e = catalog_entry(res, node.name, real)
        ov = config.read_overrides()
        models = dict(ov.get("models", {}))
        models[model] = e
        ov["models"] = models
        config.write_overrides(ov)
        teil = f", PARTIAL OFFLOAD bei {e['partial_offload']}" if e.get("partial_offload") else ""
        log.info("measured %s on %s (%s): weights %.2f GiB, kv %.4f GiB/1k, Punkte %s%s", model, node.name,
                 "nvidia-smi" if real else "/api/ps", e["weights_gib"], e["kv_gib_per_1k"],
                 {c: round(v[0], 2) for c, v in sorted(res.items())}, teil)
        state.remember({"event": "measure", "node": node.name, "model": model, "weights_gib": e["weights_gib"],
                        "kv_gib_per_1k": e["kv_gib_per_1k"], "real": real})
        state.MEASURING[model]["step"] = "fertig"
    except Exception as e:  # noqa: BLE001
        log.warning("measure %s on %s failed: %s", model, node.name, e)
        state.MEASURING[model].update(step="fehler", error=str(e))
    finally:
        node.inflight -= 1
        node.finish_load(model)
        await _release_node(node, model, "measure")
        state.MEASURING.pop(model, None)


async def handle_measure(request):
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
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
