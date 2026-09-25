"""Stufe 4 (design/roadmap.md): Observability - /metrics im Prometheus-Textformat und Usage-Erfassung.

Kein prometheus_client (keine neue Abhaengigkeit auf dem CT): Zaehler und Histogramme liegen in state.METRICS, Gauges
werden beim Abruf aus dem Laufzeitzustand gelesen. Alle Namen tragen das Praefix `skirnir_`.

Usage: je Tag und Client (dazu je Rolle und je Modell) Anfragen, Prompt-/Antwort-Tokens, Fehler - persistiert in
usage.json neben perf.json (90 Tage), sichtbar unter /admin/usage und als HA-Sensoren (requests_today, tokens_today).

Zaehler und Histogramme sind seit 0.1.9 ebenfalls persistent (metrics.json neben der Config, alle METRICS_SAVE_S und beim
Stopp): ein Deploy setzt sie nicht mehr auf null, die UI zeigt "gesamt" seit `since`. Fuer Verlaeufe ueber Tage bleibt ein
Prometheus, der /metrics abfragt, der richtige Ort - der Router ist keine Zeitreihendatenbank.
"""

import json
import os
import time

from . import admission, perf, scheduler, state
from .common import VERSION, log

DURATION_BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)
TTFT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 15)
QUEUE_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120)
USAGE_DAYS = 90
METRICS_SAVE_S = 60


def _labels(d):
    return "{" + ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in d.items()) + "}"


def _dirty():
    if not state.METRICS_DIRTY[0]:
        state.METRICS_DIRTY[0] = time.time()


def _counter(name, labels, inc=1.0):
    c = state.METRICS["counters"].setdefault(name, {})
    key = tuple(sorted(labels.items()))
    c[key] = c.get(key, 0.0) + inc
    _dirty()


def _hist(name, labels, buckets, value):
    h = state.METRICS["hist"].setdefault(name, {"buckets": buckets, "series": {}})
    key = tuple(sorted(labels.items()))
    s = h["series"].setdefault(key, {"counts": [0] * len(buckets), "sum": 0.0, "n": 0})
    for i, b in enumerate(buckets):
        if value <= b:
            s["counts"][i] += 1
    s["sum"] += value
    s["n"] += 1
    _dirty()


def count_event(entry):
    """Von state.remember aufgerufen: Ereignisse (busy, free, wol, breaker, queued, admission_timeout, no_node, ...) zaehlen."""
    ev = entry.get("event")
    if not ev or ev == "route":
        return
    labels = {"event": ev}
    if entry.get("node"):
        labels["node"] = entry["node"]
    if entry.get("state"):
        labels["state"] = entry["state"]
    _counter("skirnir_events_total", labels)


def observe_request(info, outcome, duration_s, ttft_s, prompt_tokens, completion_tokens, queued_s, cost_chf=0.0):
    """Am Ende jeder Anfrage aus proxy.dispatch."""
    client = info.get("client") or "-"
    base = {"role": info["role"], "node": info["node"], "model": info["model"], "client": client, "via": info["via"], "outcome": outcome}
    _counter("skirnir_requests_total", base)
    if cost_chf:
        _counter("skirnir_cloud_cost_chf_total", {"node": info["node"], "model": info["model"], "client": client, "role": info["role"]}, cost_chf)
    _hist("skirnir_request_duration_seconds", {"role": info["role"], "outcome": outcome}, DURATION_BUCKETS, duration_s)
    if ttft_s is not None:
        _hist("skirnir_ttft_seconds", {"model": info["model"], "node": info["node"]}, TTFT_BUCKETS, max(0.0, ttft_s))
    if queued_s:
        _hist("skirnir_queue_wait_seconds", {"priority": info.get("priority") or "normal"}, QUEUE_BUCKETS, queued_s)
    tl = {"model": info["model"], "node": info["node"], "client": client, "role": info["role"]}
    if prompt_tokens:
        _counter("skirnir_tokens_total", {**tl, "kind": "prompt"}, prompt_tokens)
    if completion_tokens:
        _counter("skirnir_tokens_total", {**tl, "kind": "completion"}, completion_tokens)
    usage_add(client, info["role"], info["model"], outcome, prompt_tokens or 0, completion_tokens or 0, cost_chf)


# ---------------- Usage ----------------

def usage_path():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "usage.json")


def metrics_path():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "metrics.json")


def _key_out(key):
    return [list(kv) for kv in key]


def _key_in(rows):
    return tuple(tuple(kv) for kv in rows)


def metrics_load():
    """Beim Start: Zaehler und Histogramme aus metrics.json uebernehmen. Liefert die Zahl der Reihen (0 = Datei fehlt)."""
    state.METRICS.setdefault("since", time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        with open(metrics_path(), encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return 0
    except Exception as e:  # noqa: BLE001
        log.warning("metrics.json unlesbar: %s", e)
        return 0
    n = 0
    for name, rows in (d.get("counters") or {}).items():
        c = state.METRICS["counters"].setdefault(name, {})
        for labels, v in rows:
            c[_key_in(labels)] = c.get(_key_in(labels), 0.0) + float(v)
            n += 1
    for name, h in (d.get("hist") or {}).items():
        buckets = tuple(h.get("buckets") or ())
        hh = state.METRICS["hist"].setdefault(name, {"buckets": buckets, "series": {}})
        if tuple(hh["buckets"]) != buckets:   # Buckets im Code geaendert: alte Reihen passen nicht mehr, weglassen
            continue
        for labels, s in h.get("series") or []:
            if len(s.get("counts") or []) != len(buckets):
                continue
            hh["series"][_key_in(labels)] = {"counts": [int(x) for x in s["counts"]], "sum": float(s["sum"]), "n": int(s["n"])}
            n += 1
    if d.get("since"):
        state.METRICS["since"] = d["since"]
    return n


def metrics_save():
    """Atomar nach metrics.json (tick_loop alle METRICS_SAVE_S bei Aenderung, app beim Stopp)."""
    try:
        d = {"since": state.METRICS.get("since"), "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "counters": {name: [[_key_out(k), v] for k, v in c.items()] for name, c in state.METRICS["counters"].items()},
             "hist": {name: {"buckets": list(h["buckets"]), "series": [[_key_out(k), s] for k, s in h["series"].items()]}
                      for name, h in state.METRICS["hist"].items()}}
        tmp = metrics_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, metrics_path())
    except Exception as e:  # noqa: BLE001
        log.warning("metrics.json schreiben: %s", e)


def usage_load():
    try:
        with open(usage_path(), encoding="utf-8") as f:
            state.USAGE.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("usage.json unlesbar: %s", e)
    state.USAGE.setdefault("days", {})


def usage_save():
    try:
        days = state.USAGE.setdefault("days", {})
        for d in sorted(days)[:-USAGE_DAYS]:
            days.pop(d, None)
        tmp = usage_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state.USAGE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, usage_path())
    except Exception as e:  # noqa: BLE001
        log.warning("usage.json schreiben: %s", e)


def _bump(d, outcome, p, c, cost=0.0):
    d["requests"] = d.get("requests", 0) + 1
    d["prompt_tokens"] = d.get("prompt_tokens", 0) + p
    d["completion_tokens"] = d.get("completion_tokens", 0) + c
    if cost:
        d["cost_chf"] = round(d.get("cost_chf", 0.0) + cost, 6)
    if outcome != "ok":
        d["errors"] = d.get("errors", 0) + 1


def usage_add(client, role, model, outcome, p, c, cost=0.0):
    day = state.USAGE.setdefault("days", {}).setdefault(time.strftime("%Y-%m-%d"), {"clients": {}})
    cl = day["clients"].setdefault(client, {"roles": {}, "models": {}})
    _bump(cl, outcome, p, c, cost)
    _bump(cl["roles"].setdefault(role, {}), outcome, p, c, cost)
    _bump(cl["models"].setdefault(model, {}), outcome, p, c, cost)
    state.USAGE_DIRTY[0] = time.time()


def usage_today():
    return state.USAGE.get("days", {}).get(time.strftime("%Y-%m-%d"), {"clients": {}})


def usage_totals(day):
    t = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "errors": 0, "cost_chf": 0.0}
    for cl in day.get("clients", {}).values():
        for k in t:
            t[k] += cl.get(k, 0)
    t["cost_chf"] = round(t["cost_chf"], 6)
    return t


def usage_view():
    days = state.USAGE.get("days", {})
    today = usage_today()
    return {"today": {"date": time.strftime("%Y-%m-%d"), **usage_totals(today), "clients": today.get("clients", {})},
            "days": {d: usage_totals(v) for d, v in sorted(days.items())[-30:]},
            "since": min(days) if days else None}


# ---------------- Prometheus-Text ----------------

def render():
    now = time.time()
    out = []

    def gauge(name, help_, rows):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} gauge")
        for labels, v in rows:
            if v is None:
                continue
            out.append(f"{name}{_labels(labels) if labels else ''} {v}")

    gauge("skirnir_info", "router version", [({"version": VERSION}, 1)])
    nodes = list(state.NODES.values())
    gauge("skirnir_node_up", "1 = node online (free or busy)", [({"node": n.name}, 1 if n.state != "offline" else 0) for n in nodes])
    gauge("skirnir_node_info", "Ollama version per node", [({"node": n.name, "ollama_version": n.ollama_version or "?"}, 1) for n in nodes])
    gauge("skirnir_node_state", "1 for the current node state",
          [({"node": n.name, "state": s}, 1 if n.state == s else 0) for n in nodes for s in ("free", "busy", "offline")])
    gauge("skirnir_node_inflight", "running requests per node", [({"node": n.name}, n.inflight) for n in nodes])
    gauge("skirnir_node_max_inflight", "admission limit per node", [({"node": n.name}, n.effective_max_inflight()) for n in nodes])
    gauge("skirnir_node_gpu_util_percent", "GPU utilisation (agent)", [({"node": n.name}, n.gpu_util if n.gpu_known(now) else None) for n in nodes])
    gauge("skirnir_node_vram_free_gib", "free VRAM", [({"node": n.name}, round(n.vram_free_gib, 3) if n.gpu_known(now) and n.vram_free_gib is not None else None) for n in nodes])
    gauge("skirnir_node_vram_used_gib", "used VRAM", [({"node": n.name}, round(n.vram_used_gib, 3) if n.gpu_known(now) and n.vram_used_gib is not None else None) for n in nodes])
    gauge("skirnir_node_vram_total_gib", "total VRAM", [({"node": n.name}, n.vram_total_gib) for n in nodes])
    gauge("skirnir_node_vram_foreign_gib", "foreign VRAM (desktop, game) above the baseline", [({"node": n.name}, round(n.foreign_vram_gib(), 3)) for n in nodes])
    gauge("skirnir_node_ollama_vram_gib", "VRAM used by Ollama (/api/ps)", [({"node": n.name}, round(n.ollama_vram_gib(), 3)) for n in nodes])
    gauge("skirnir_node_breaker", "1 for the breaker state", [({"node": n.name, "state": s}, 1 if n.breaker == s else 0) for n in nodes for s in ("closed", "open", "half_open")])
    # Sensoren (Agent >= 0.6.0) und GPU-Schutz (>= 0.7.0)
    sens = lambda n, k: (n.sensors or {}).get(k) if n.gpu_known(now) else None  # noqa: E731
    gauge("skirnir_node_gpu_power_w", "board power draw of the card", [({"node": n.name}, sens(n, "power_w")) for n in nodes])
    gauge("skirnir_node_gpu_power_limit_w", "effective power limit", [({"node": n.name}, sens(n, "power_limit_w")) for n in nodes])
    gauge("skirnir_node_gpu_pin16_power_w", "power through the 16-pin connector (GPU-Z)", [({"node": n.name}, sens(n, "pin16_power_w")) for n in nodes])
    gauge("skirnir_node_gpu_pin16_v", "voltage at the 16-pin connector (GPU-Z)", [({"node": n.name}, sens(n, "pin16_voltage_v")) for n in nodes])
    gauge("skirnir_node_gpu_temp_c", "GPU temperature", [({"node": n.name}, sens(n, "temp_c")) for n in nodes])
    gauge("skirnir_node_gpu_mem_temp_c", "memory temperature (GPU-Z)", [({"node": n.name}, sens(n, "mem_temp_c")) for n in nodes])
    gauge("skirnir_node_gpu_guard", "1 for the GPU guard state", [({"node": n.name, "state": s}, 1 if n.guard_state(now) == s else 0) for n in nodes for s in ("aus", "unverfuegbar", "normal", "hochlast", "gedrosselt", "erholung")])
    gauge("skirnir_model_loaded_gib", "loaded models and their VRAM", [({"node": n.name, "model": m}, round(g, 3)) for n in nodes for m, g in n.loaded.items()])
    adm = admission.view()
    gauge("skirnir_admission_waiting", "waiting requests per node", [({"node": k}, v) for k, v in adm["by_node"].items()] or [({}, 0)])
    gauge("skirnir_sessions", "active session affinities", [({}, len(state.SESSIONS))])
    rows = []
    for _exposed, role in state.CFG.roles.items():
        pick = scheduler.choose(role, role["tiers"], None, now, mutate=False)   # Sicht: /metrics wechselt keinen Breaker
        rows.append(({"role": role["name"]}, 1 if pick else 0))
    gauge("skirnir_role_ready", "1 = role could be served right now", rows)
    gauge("skirnir_perf_gen_tps", "EWMA generation speed tok/s per model@node",
          [({"model": e["model"], "node": e["node"]}, (e.get("ewma") or {}).get("gen_tps")) for e in state.PERF.values() if isinstance(e, dict) and e.get("model")])
    gauge("skirnir_perf_error_rate", "share of non-ok outcomes in the last 20 results",
          [({"model": e["model"], "node": e["node"]}, round(perf.error_rate(e["model"], e["node"]), 3)) for e in state.PERF.values() if isinstance(e, dict) and e.get("model")])
    ca = state.CLIENT_STATS
    gauge("skirnir_client_forbidden_total", "rejected requests per client (403)", [({"client": k}, v.get("forbidden", 0)) for k, v in ca["clients"].items()])
    gauge("skirnir_client_rate_limited_total", "rate-limited requests per client (429)", [({"client": k}, v.get("rate_limited", 0)) for k, v in ca["clients"].items()])
    gauge("skirnir_unauthenticated_total", "requests without a valid identity per source IP", [({"ip": k}, v.get("total", 0)) for k, v in ca["unauth"].items()])
    t = usage_totals(usage_today())
    gauge("skirnir_usage_today_requests", "requests today (all clients)", [({}, t["requests"])])
    from . import cloud
    gauge("skirnir_cloud_spend_month_chf", "cloud spend in the current month per provider", [({"provider": p}, round(cloud.spend_month(p), 4)) for p in state.CLOUD])
    gauge("skirnir_cloud_budget_month_chf", "monthly budget per provider", [({"provider": p}, float(t_.spec.get("budget_month_chf") or 0)) for p, t_ in state.CLOUD.items()])
    gauge("skirnir_cloud_enabled", "1 = provider enabled and has a key", [({"provider": p}, 1 if (t_.spec.get("enabled", True) and t_.api_key) else 0) for p, t_ in state.CLOUD.items()])
    gauge("skirnir_usage_today_tokens", "tokens today", [({"kind": "prompt"}, t["prompt_tokens"]), ({"kind": "completion"}, t["completion_tokens"])])

    for name, series in state.METRICS["counters"].items():
        out.append(f"# TYPE {name} counter")
        for key, v in series.items():
            out.append(f"{name}{_labels(dict(key))} {v:g}")
    for name, h in state.METRICS["hist"].items():
        out.append(f"# TYPE {name} histogram")
        for key, s in h["series"].items():
            lab = dict(key)
            for i, b in enumerate(h["buckets"]):
                out.append(f"{name}_bucket{_labels({**lab, 'le': b})} {s['counts'][i]}")
            out.append(f"{name}_bucket{_labels({**lab, 'le': '+Inf'})} {s['n']}")
            out.append(f"{name}_sum{_labels(lab)} {s['sum']:.3f}")
            out.append(f"{name}_count{_labels(lab)} {s['n']}")
    return "\n".join(out) + "\n"
