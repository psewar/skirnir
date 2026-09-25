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
        return web.json_response({"error": "model fehlt"}, status=400)
    if state.BENCHING or state.MEASURING:
        return web.json_response({"error": "es laeuft schon eine Messung/ein Benchmark, bitte warten"}, status=409)
    node = _free_node_for(model, b.get("node"), prefer_loaded=True)
    if node is None:
        return web.json_response({"error": f"kein freier Knoten ohne laufende Anfragen hat {model}"}, status=409)
    state.spawn(bench_model(model, node))
    return web.json_response({"started": True, "node": node.name, "model": model})


async def measure_model(model, node):
    """Modell bei num_ctx 8192 und 32768 laden, /api/ps auslesen -> weights_gib + kv_gib_per_1k, in roles.yaml speichern.
    Laeuft nur auf einem freien Knoten ohne laufende Anfragen; verdraengt dabei ggf. warme Modelle (danach prewarm)."""
    state.MEASURING[model] = {"node": node.name, "step": "start", "started": time.time(), "error": None}
    node.inflight += 1          # kein busy-Fehlalarm durch unsere eigene GPU-Last, Router waehlt den Knoten nicht fuer anderes
    res = {}
    try:
        for ctx in (8192, 32768):
            state.MEASURING[model]["step"] = f"lade @{ctx}"
            node.announce_load(model, ctx)
            body = {"model": model, "keep_alive": "2m", "options": {"num_ctx": ctx}}
            async with nodes.nreq(node, "post", "/api/generate", json=body, timeout=ClientTimeout(total=900)) as r:
                if r.status >= 400:
                    raise RuntimeError(f"Ollama {r.status}: {(await r.text())[:200]}")
            async with nodes.nreq(node, "get", "/api/ps", timeout=ClientTimeout(total=10)) as r:
                ps = await r.json()
            want = node.digest_of.get(model)
            entry = next((m for m in ps.get("models", []) if m.get("name") == model or (want and m.get("digest") == want)), None)
            if not entry:
                raise RuntimeError(f"Modell nach dem Laden nicht in /api/ps (ctx {ctx})")
            res[ctx] = (entry.get("size_vram", 0) / GIB, entry.get("size", 0) / GIB, entry.get("context_length"))
        a, b = res[8192][0], res[32768][0]
        kv = max(0.0, (b - a) / 24.0)
        weights = max(0.1, a - kv * 8.0)
        partial = any(sv + 0.05 < st for sv, st, _ in res.values())
        ov = config.read_overrides()
        models = dict(ov.get("models", {}))
        models[model] = {"weights_gib": round(weights, 2), "kv_gib_per_1k": round(kv, 4), "source": "gemessen",
                         "measured_at": time.strftime("%Y-%m-%d %H:%M"), "measured_on": node.name,
                         "vram_gib_8k": round(a, 2), "vram_gib_32k": round(b, 2),
                         **({"partial_offload": True} if partial else {})}
        ov["models"] = models
        config.write_overrides(ov)
        log.info("measured %s on %s: weights %.2f GiB, kv %.4f GiB/1k (8k=%.2f, 32k=%.2f%s)", model, node.name, weights, kv, a, b,
                 ", PARTIAL OFFLOAD" if partial else "")
        state.remember({"event": "measure", "node": node.name, "model": model, "weights_gib": round(weights, 2), "kv_gib_per_1k": round(kv, 4)})
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
        return web.json_response({"error": "model fehlt"}, status=400)
    if model in state.MEASURING:
        return web.json_response({"error": f"{model} wird gerade gemessen"}, status=409)
    if len(state.MEASURING) >= 1:
        return web.json_response({"error": "es laeuft schon eine Messung, bitte warten"}, status=409)
    node = _free_node_for(model, b.get("node"))
    if node is None:
        return web.json_response({"error": f"kein freier Knoten ohne laufende Anfragen hat {model}"}, status=409)
    state.spawn(measure_model(model, node))
    return web.json_response({"started": True, "node": node.name, "model": model})
