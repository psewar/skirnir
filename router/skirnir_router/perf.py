"""Leistungsdaten (passiv + Benchmark) und das Vermessen von Modellen fuer den Katalog."""

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
        log.warning("perf.json ist kein Mapping - ignoriert")


def perf_save():
    save_json_file(perf_path(), state.PERF, "perf.json", ensure_ascii=False, indent=1)


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def perf_stats(stats, ttft_s):
    """Ollama-Antwortfelder (Nanosekunden) -> tok/s und Millisekunden. None wenn unbrauchbar."""
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
    """Ollama auf dem Knoten fragen (Tunnel oder direkt): die JSON-Antwort als dict oder ein Problem 502 mit dem Grund.
    answer=False: nur Erfolg zaehlt, der Koerper wird gelesen und verworfen ({}) - /api/generate zum Laden/Entladen
    streamt NDJSON (mehrere Zeilen), das ist kein einzelnes JSON-Dokument."""
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
    return data if isinstance(data, dict) else bad_gateway(f"Ollama {path}: keine JSON-Antwort")


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
        runs = await _bench_runs(model, node, ctx)
        if failed(runs):
            log.warning("bench %s on %s failed: %s", model, node.name, runs.message)
            state.BENCHING[model].update(step="fehler", error=runs.message)
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
        state.BENCHING[model]["step"] = "fertig"
    finally:
        node.inflight -= 1
        await _release_node(node, model, "bench")
        state.BENCHING.pop(model, None)


async def _bench_runs(model, node, ctx):
    """Aufwaermlauf + 2 Messlaeufe: die beiden Messungen oder ein Problem."""
    body = {"model": model, "stream": False, "keep_alive": "5m", "options": {"num_ctx": ctx, "num_predict": 200, "temperature": 0, "seed": 42},
            "messages": [{"role": "user", "content": BENCH_PROMPT}]}
    runs = []
    for i in range(3):
        state.BENCHING[model]["step"] = "aufwärmen" if i == 0 else f"Messlauf {i}/2"
        t0 = time.time()
        data = await _ollama(node, "post", "/api/chat", body, timeout=900)
        if failed(data):
            return data
        wall = time.time() - t0
        s = perf_stats(data, None)
        if not s:
            return bad_gateway("keine eval-Statistik in der Antwort")
        # TTFT-Naeherung im Non-Stream-Modus: Wandzeit - Generierzeit - Ladezeit
        s["ttft_ms"] = max(0, round((wall - (data.get("eval_duration") or 0) / 1e9 - (data.get("load_duration") or 0) / 1e9) * 1000))
        if i > 0:
            runs.append(s)
    return runs


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
        await _ollama(node, "post", "/api/generate", {"model": model, "keep_alive": 0}, answer=False)   # best effort: der Poll danach zeigt, was geladen blieb
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
    """Geladene Modelle laut /api/ps (Liste von dicts) oder ein Problem."""
    data = await _ollama(node, "get", "/api/ps", timeout=10)
    if failed(data):
        return data
    models = data.get("models") or []
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else bad_gateway("Ollama /api/ps: models ist keine Liste")


async def _ps_entry(node, model):
    """Eintrag des Modells in /api/ps, None wenn nicht geladen, oder ein Problem."""
    models = await _ps_models(node)
    return models if failed(models) else next((m for m in models if _is_model(node, m, model)), None)


async def _load(node, model, ctx):
    node.announce_load(model, ctx)
    loaded = await _ollama(node, "post", "/api/generate", {"model": model, "keep_alive": "2m", "options": {"num_ctx": ctx}}, timeout=900, answer=False)
    entry = loaded if failed(loaded) else await _ps_entry(node, model)
    if entry is None:
        return bad_gateway(f"Modell nach dem Laden nicht in /api/ps (ctx {ctx})")
    return entry


async def _unload_all(node):
    """Alles entladen und warten, bis /api/ps leer ist - sonst misst der Zuwachs ein anderes Modell mit. Liefert, was
    /api/ps vorher als size_vram meldete (GiB): so viel muss nvidia-smi danach mindestens freigeben. Oder ein Problem."""
    geladen = await _ps_models(node)
    if failed(geladen):
        return geladen
    for m in geladen:
        unloaded = await _ollama(node, "post", "/api/generate", {"model": m.get("name"), "keep_alive": 0}, answer=False)
        if failed(unloaded):
            return unloaded
    for _ in range(60):
        still = await _ps_models(node)
        if failed(still):
            return still
        if not still:
            return sum(m.get("size_vram", 0) for m in geladen) / GIB
        await asyncio.sleep(1)
    return unavailable("Ollama entlaedt nicht (nach 60 s noch Modelle in /api/ps)")


async def _empty_used(node, before, unloaded, timeout=120):
    """Grundlast nach dem Entladen. /api/ps ist sofort leer, der Speicher wird aber erst Sekunden spaeter frei - zwei
    gleiche Heartbeats hintereinander bewiesen das nicht (0.3.5: nach dem Entladen von 26 GiB las die Messung "leer" bei
    noch voller Karte, der erste Messpunkt wurde negativ). Darum erst warten, bis nvidia-smi mindestens 90 % dessen
    freigegeben hat, was /api/ps vorher meldete, dann die ruhige Belegung ablesen. Oder ein Problem."""
    if before is not None and unloaded > 0.5:
        ziel = before - 0.9 * unloaded
        deadline = time.time() + timeout
        while node.vram_used_gib is None or node.vram_used_gib > ziel:
            if time.time() > deadline:
                return unavailable(f"Speicher wird nach dem Entladen nicht frei ({node.vram_used_gib} GiB, erwartet <= {ziel:.2f})")
            await asyncio.sleep(0.5)
    await asyncio.sleep(state.CFG.vram_settle_s)
    return await _stable_used(node, time.time())


async def _stable_used(node, after, timeout=90):
    """Belegung laut nvidia-smi (Heartbeat), sobald sie steht: zwei Heartbeats nach `after`, < 0,1 GiB auseinander.
    Oder ein Problem, wenn sie in `timeout` nicht zur Ruhe kommt."""
    last, last_ts = None, after
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        if node.hb_ts and node.hb_ts > last_ts and node.vram_used_gib is not None:
            if last is not None and abs(node.vram_used_gib - last) < 0.1:
                return node.vram_used_gib
            last, last_ts = node.vram_used_gib, node.hb_ts
    return unavailable("VRAM-Belegung kam nicht zur Ruhe (Heartbeats)")


async def _measure_real(model, node):
    """Seit 0.3.5: je Kontext alles entladen, Grundlast ablesen, laden, Zuwachs laut nvidia-smi = echte Belegung.
    /api/ps meldet nur einen Teil (qwen3.6:35b-a3b 21,0 statt 26,4 GiB bei 196k, laguna-xs-2.1 19,2 statt 29,7 GiB bei
    131k - gemessen 2026-09-30); mit diesen Zahlen verrechnete sich der Router bei Budget, fremdem VRAM und Grundlast.
    Liefert {ctx: (belegt, ps_size_vram, ps_size)} oder ein Problem."""
    res = {}
    for ctx in measure_ctxs(model):
        for versuch in range(1, MEASURE_TRIES + 1):
            punkt = await _measure_point(model, node, ctx)
            if failed(punkt):
                return punkt
            if isinstance(punkt, tuple):
                res[ctx] = punkt
                break
            log.info("measure %s@%d on %s: Versuch %d verworfen (%s)", model, ctx, node.name, versuch, punkt)
        else:
            return unavailable(f"Messung @{ctx} nach {MEASURE_TRIES} Versuchen nicht sauber: {punkt}")
    return res


MEASURE_TRIES = 3


def _is_model(node, m, model):
    want = node.digest_of.get(model)
    return m.get("name") == model or (want is not None and m.get("digest") == want)


async def _others_loading(node, model, timeout=300):
    """Warten, bis kein ANDERER Ladevorgang laeuft (prewarm, echte Anfrage). 0.3.5: das prewarm nach der vorigen Messung
    lud qwen3.6 genau dann, als die naechste Messung ihre Grundlast las - /api/ps war noch leer, nichts wurde entladen.
    None, sobald nichts anderes mehr laedt, oder ein Problem."""
    deadline = time.time() + timeout
    while any(m != model and deadline_m > time.time() for m, (_g, deadline_m) in node.loading.items()):
        if time.time() > deadline:
            return unavailable("anderer Ladevorgang endet nicht")
        await asyncio.sleep(1)
    return None


async def _measure_point(model, node, ctx):
    """Ein Messpunkt. Liefert (belegt, ps_vram, ps_size) oder einen Grund, warum er verworfen wurde (dann neu versuchen):
    ein anderes Modell kam dazwischen, oder der Zuwachs ist kleiner als Ollamas eigene Meldung. Ein Problem bricht die
    Messung ab."""
    waited = await _others_loading(node, model)
    if failed(waited):
        return waited
    state.MEASURING[model]["step"] = f"entlade fuer @{ctx}"
    vorher = node.vram_used_gib
    unloaded = await _unload_all(node)
    leer = unloaded if failed(unloaded) else await _empty_used(node, vorher, unloaded)
    if failed(leer):
        return leer
    state.MEASURING[model]["step"] = f"lade @{ctx}"
    entry = await _load(node, model, ctx)
    voll = entry if failed(entry) else await _stable_used(node, time.time())
    geladen = voll if failed(voll) else await _ps_models(node)
    if failed(geladen):
        return geladen
    fremd = [m.get("name") for m in geladen if not _is_model(node, m, model)]
    if fremd or any(m != model for m in node.loading):
        return f"anderes Modell geladen: {fremd or list(node.loading)}"
    belegt, sv = voll - leer, entry.get("size_vram", 0) / GIB
    # Plausibel: nie weniger als das, was Ollama selbst meldet - sonst war die Grundlast falsch, und der Katalog
    # bekaeme Unsinn (0.3.5: gemma4:26b "-7,67 GiB" bei 8k). Lieber keine Messung als eine falsche.
    if belegt < sv - 0.3:
        return f"unplausibel: nvidia-smi-Zuwachs {belegt:.2f} GiB < /api/ps {sv:.2f} GiB"
    return belegt, sv, entry.get("size", 0) / GIB


async def _measure_ps(model, node):
    """Rueckfall ohne GPU-Werte im Heartbeat (bis 0.3.4 der einzige Weg): /api/ps bei 8k und 32k."""
    res = {}
    for ctx in MEASURE_CTX_FIX:
        state.MEASURING[model]["step"] = f"lade @{ctx}"
        entry = await _load(node, model, ctx)
        if failed(entry):
            return entry
        res[ctx] =(entry.get("size_vram", 0) / GIB, entry.get("size_vram", 0) / GIB, entry.get("size", 0) / GIB)
    return res


def catalog_entry(res, node_name, real):
    """Messergebnis {ctx: (belegt, ps_vram, ps_size)} -> Katalog-Eintrag. Bei Teil-Auslagerung in den RAM misst
    nvidia-smi nur den GPU-Teil; der ausgelagerte Rest (size - size_vram) kommt dazu, damit der Katalog den ganzen Bedarf
    kennt und der Router das Modell bei diesem Kontext nicht mehr fuer passend haelt (0.3.5 liess solche Punkte weg:
    granite4.2:30b stand danach mit flachen 21 GiB im Katalog, obwohl es bei 32k nicht auf die Karte passt)."""
    partial = sorted(ctx for ctx, (_b, sv, st) in res.items() if sv + 0.05 < st)
    points = [(ctx, b + (max(0.0, st - sv) if ctx in partial else 0.0)) for ctx, (b, sv, st) in res.items()]
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
        if failed(res):
            log.warning("measure %s on %s failed: %s", model, node.name, res.message)
            state.MEASURING[model].update(step="fehler", error=res.message)
            return
        e = catalog_entry(res, node.name, real)
        ov = config.read_overrides()
        models = dict(ov.get("models", {}))
        models[model] = e
        ov["models"] = models
        written = config.write_overrides(ov)
        if failed(written):   # Messung gelungen, Katalog nicht geschrieben: als Fehler melden, nicht als fertig
            log.warning("measure %s on %s: catalog not written: %s", model, node.name, written.message)
            state.MEASURING[model].update(step="fehler", error=written.message)
            return
        teil = f", PARTIAL OFFLOAD bei {e['partial_offload']}" if e.get("partial_offload") else ""
        log.info("measured %s on %s (%s): weights %.2f GiB, kv %.4f GiB/1k, Punkte %s%s", model, node.name,
                 "nvidia-smi" if real else "/api/ps", e["weights_gib"], e["kv_gib_per_1k"],
                 {c: round(v[0], 2) for c, v in sorted(res.items())}, teil)
        state.remember({"event": "measure", "node": node.name, "model": model, "weights_gib": e["weights_gib"],
                        "kv_gib_per_1k": e["kv_gib_per_1k"], "real": real})
        state.MEASURING[model]["step"] = "fertig"
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
