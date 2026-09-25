"""Stufe 4 (design/roadmap.md): Observability - /metrics im Prometheus-Textformat und Usage-Erfassung.

Kein prometheus_client (keine neue Abhaengigkeit auf dem CT): Zaehler und Histogramme liegen in state.METRICS, Gauges
werden beim Abruf aus dem Laufzeitzustand gelesen. Alle Namen tragen das Praefix `skirnir_`.

Usage: je Tag und Client (dazu je Rolle und je Modell) Anfragen, Prompt-/Antwort-Tokens, Fehler - persistiert in
usage.json neben perf.json (90 Tage), sichtbar unter /admin/usage und als HA-Sensoren (requests_today, tokens_today).
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


def _labels(d):
    return "{" + ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in d.items()) + "}"


def _counter(name, labels, inc=1.0):
    c = state.METRICS["counters"].setdefault(name, {})
    key = tuple(sorted(labels.items()))
    c[key] = c.get(key, 0.0) + inc


def _hist(name, labels, buckets, value):
    h = state.METRICS["hist"].setdefault(name, {"buckets": buckets, "series": {}})
    key = tuple(sorted(labels.items()))
    s = h["series"].setdefault(key, {"counts": [0] * len(buckets), "sum": 0.0, "n": 0})
    for i, b in enumerate(buckets):
        if value <= b:
            s["counts"][i] += 1
    s["sum"] += value
    s["n"] += 1


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

    gauge("skirnir_info", "Router-Version", [({"version": VERSION}, 1)])
    nodes = list(state.NODES.values())
    gauge("skirnir_node_up", "1 = Knoten online (free oder busy)", [({"node": n.name}, 1 if n.state != "offline" else 0) for n in nodes])
    gauge("skirnir_node_info", "Ollama-Version je Knoten", [({"node": n.name, "ollama_version": n.ollama_version or "?"}, 1) for n in nodes])
    gauge("skirnir_node_state", "1 fuer den aktuellen Zustand des Knotens",
          [({"node": n.name, "state": s}, 1 if n.state == s else 0) for n in nodes for s in ("free", "busy", "offline")])
    gauge("skirnir_node_inflight", "laufende Anfragen je Knoten", [({"node": n.name}, n.inflight) for n in nodes])
    gauge("skirnir_node_max_inflight", "Admission-Grenze je Knoten", [({"node": n.name}, n.effective_max_inflight()) for n in nodes])
    gauge("skirnir_node_gpu_util_percent", "GPU-Auslastung (Agent)", [({"node": n.name}, n.gpu_util if n.gpu_known(now) else None) for n in nodes])
    gauge("skirnir_node_vram_free_gib", "freies VRAM", [({"node": n.name}, round(n.vram_free_gib, 3) if n.gpu_known(now) and n.vram_free_gib is not None else None) for n in nodes])
    gauge("skirnir_node_vram_used_gib", "belegtes VRAM", [({"node": n.name}, round(n.vram_used_gib, 3) if n.gpu_known(now) and n.vram_used_gib is not None else None) for n in nodes])
    gauge("skirnir_node_vram_total_gib", "VRAM gesamt", [({"node": n.name}, n.vram_total_gib) for n in nodes])
    gauge("skirnir_node_vram_foreign_gib", "fremdes VRAM (Desktop, Spiel) ueber der Baseline", [({"node": n.name}, round(n.foreign_vram_gib(), 3)) for n in nodes])
    gauge("skirnir_node_ollama_vram_gib", "von Ollama belegtes VRAM (/api/ps)", [({"node": n.name}, round(n.ollama_vram_gib(), 3)) for n in nodes])
    gauge("skirnir_node_breaker", "1 fuer den Breaker-Zustand", [({"node": n.name, "state": s}, 1 if n.breaker == s else 0) for n in nodes for s in ("closed", "open", "half_open")])
    # Sensoren (Agent >= 0.6.0) und GPU-Schutz (>= 0.7.0)
    sens = lambda n, k: (n.sensors or {}).get(k) if n.gpu_known(now) else None  # noqa: E731
    gauge("skirnir_node_gpu_power_w", "Leistungsaufnahme der Karte (Board Power)", [({"node": n.name}, sens(n, "power_w")) for n in nodes])
    gauge("skirnir_node_gpu_power_limit_w", "wirksames Power-Limit", [({"node": n.name}, sens(n, "power_limit_w")) for n in nodes])
    gauge("skirnir_node_gpu_pin16_power_w", "Leistung durch den 16-Pin-Stecker (GPU-Z)", [({"node": n.name}, sens(n, "pin16_power_w")) for n in nodes])
    gauge("skirnir_node_gpu_pin16_v", "Spannung am 16-Pin-Stecker (GPU-Z)", [({"node": n.name}, sens(n, "pin16_voltage_v")) for n in nodes])
    gauge("skirnir_node_gpu_temp_c", "GPU-Temperatur", [({"node": n.name}, sens(n, "temp_c")) for n in nodes])
    gauge("skirnir_node_gpu_mem_temp_c", "Speichertemperatur (GPU-Z)", [({"node": n.name}, sens(n, "mem_temp_c")) for n in nodes])
    gauge("skirnir_node_gpu_guard", "1 fuer den Zustand des GPU-Schutzes", [({"node": n.name, "state": s}, 1 if n.guard_state(now) == s else 0) for n in nodes for s in ("aus", "unverfuegbar", "normal", "hochlast", "gedrosselt", "erholung")])
    gauge("skirnir_model_loaded_gib", "geladene Modelle und ihre VRAM-Belegung", [({"node": n.name, "model": m}, round(g, 3)) for n in nodes for m, g in n.loaded.items()])
    adm = admission.view()
    gauge("skirnir_admission_waiting", "wartende Anfragen je Knoten", [({"node": k}, v) for k, v in adm["by_node"].items()] or [({}, 0)])
    gauge("skirnir_sessions", "aktive Session-Affinitaeten", [({}, len(state.SESSIONS))])
    rows = []
    for _exposed, role in state.CFG.roles.items():
        pick = scheduler.choose(role, role["tiers"], None, now, mutate=False)   # Sicht: /metrics wechselt keinen Breaker
        rows.append(({"role": role["name"]}, 1 if pick else 0))
    gauge("skirnir_role_ready", "1 = Rolle koennte jetzt bedient werden", rows)
    gauge("skirnir_perf_gen_tps", "EWMA Generier-Tempo tok/s je Modell@Knoten",
          [({"model": e["model"], "node": e["node"]}, (e.get("ewma") or {}).get("gen_tps")) for e in state.PERF.values() if isinstance(e, dict) and e.get("model")])
    gauge("skirnir_perf_error_rate", "Anteil Nicht-ok der letzten 20 Ergebnisse",
          [({"model": e["model"], "node": e["node"]}, round(perf.error_rate(e["model"], e["node"]), 3)) for e in state.PERF.values() if isinstance(e, dict) and e.get("model")])
    ca = state.CLIENT_STATS
    gauge("skirnir_client_forbidden_total", "abgewiesene Anfragen je Client (403)", [({"client": k}, v.get("forbidden", 0)) for k, v in ca["clients"].items()])
    gauge("skirnir_client_rate_limited_total", "gedrosselte Anfragen je Client (429)", [({"client": k}, v.get("rate_limited", 0)) for k, v in ca["clients"].items()])
    gauge("skirnir_unauthenticated_total", "Anfragen ohne gueltige Identitaet je Quell-IP", [({"ip": k}, v.get("total", 0)) for k, v in ca["unauth"].items()])
    t = usage_totals(usage_today())
    gauge("skirnir_usage_today_requests", "Anfragen heute (alle Clients)", [({}, t["requests"])])
    from . import cloud
    gauge("skirnir_cloud_spend_month_chf", "Cloud-Ausgaben im laufenden Monat je Anbieter", [({"provider": p}, round(cloud.spend_month(p), 4)) for p in state.CLOUD])
    gauge("skirnir_cloud_budget_month_chf", "Monatsbudget je Anbieter", [({"provider": p}, float(t_.spec.get("budget_month_chf") or 0)) for p, t_ in state.CLOUD.items()])
    gauge("skirnir_cloud_enabled", "1 = Anbieter aktiv und mit Schluessel", [({"provider": p}, 1 if (t_.spec.get("enabled", True) and t_.api_key) else 0) for p, t_ in state.CLOUD.items()])
    gauge("skirnir_usage_today_tokens", "Tokens heute", [({"kind": "prompt"}, t["prompt_tokens"]), ({"kind": "completion"}, t["completion_tokens"])])

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
