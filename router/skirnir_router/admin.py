"""Control-Plane: UI, Zustand, Konfiguration, Probelauf."""

import asyncio
import hashlib
import os
import re
import secrets
import time

from aiohttp import ClientTimeout, web

from . import auth, config, perf, state, values
from . import admission, cloud, decision, metrics, ops
from .outcome import failed, invalid
from .decision.base import DecisionRequest
from . import request as request_mod
from .common import VERSION, GIB, log, read_json, split_listen


UI_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.html")   # ui.html sits next to router.py, one level above the package


async def handle_ui(request):
    if not os.path.exists(UI_PATH):
        return web.Response(text="ui.html missing", status=404)
    return web.FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


ASSETS = {"skirnir.png": "skirnir.png", "favicon.png": "favicon.png", "favicon.ico": "favicon.png"}   # Operator logo (tile since 2026-09-16; previously Raven) cut out + icon with tile


async def handle_asset(request):
    """Logo (Skirnir tile, 256 px, transparent corners) and tab icon (128 px); /favicon.ico serves the same PNG."""
    name = ASSETS.get(request.match_info["name"])
    path = os.path.join(os.path.dirname(UI_PATH), name) if name else None
    if not path or not os.path.exists(path):
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Content-Type": "image/png", "Cache-Control": "public, max-age=86400"})


async def handle_config_get(request):
    ov = config.read_overrides()
    base_models = set((state.CFG.base().get("models") or {}))
    return web.json_response({"roles": config.roles_as_config(), "models": state.CFG.models, "nodes": state.CFG.nodes,
                              "expose_concrete_models": state.CFG.expose_concrete, "modes": state.CFG.raw.get("modes", {}),
                              "warm_first": state.CFG.warm_first,
                              "settings": config.settings_view(lang=request.query.get("lang", "de")),                     # Stage 7: UI-editable settings
                              "ui_models": sorted(m for m in state.CFG.models if m not in base_models),   # Catalog entries from roles.yaml (deletable)
                              "cloud_providers": sorted(state.CFG.cloud.get("providers") or {}),
                              "clients": clients_view(),                                 # Clients-Tab: Identitaet, Herkunft, Felder
                              "overrides_file": state.CFG.overrides_path, "overrides_active": os.path.exists(state.CFG.overrides_path),
                              "overrides_settings": ov.get("settings") or {}})


CLIENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
CLIENT_FIELDS = ("ip", "roles", "models", "requests_per_minute", "max_priority", "cloud", "data_class", "note")


def _privacy_view(pc):
    """router.cloud.privacy_check for /admin/state: settings and list sizes, not the operator's terms themselves."""
    if pc is None:
        return None
    return {"enabled": pc.enabled, "endpoint": pc.endpoint, "threshold": pc.threshold, "terms": len(pc.terms),
            "keywords": pc.keywords, "redact_domains": list(pc.redact_domains), "redact_device_classes": list(pc.redact_device_classes),
            "redact_names": len(pc.redact_names), "redact_tools": list(pc.redact_tools)}


def base_clients():
    return set(((state.CFG.base().get("router") or {}).get("client_auth") or {}).get("clients") or {})


def clients_view():
    """All effective clients with origin (config.yaml / UI) and identity markers - never the full hash."""
    base = base_clients()
    ov = (config.read_overrides().get("clients") or {})
    out = {}
    out[state.INTERNAL_CLIENT] = {"source": "intern", "has_token": True, "token_prefix": "", "token_rotated": None, "created": None, "ip": ["127.0.0.1"],
                                  "roles": ["*"], "models": True, "requests_per_minute": None, "max_priority": None, "cloud": True, "data_class": None,
                                  "note": "der Router selbst: Knopf „Ausprobieren“; Secret entsteht bei jedem Start neu, nur im Speicher, nur von localhost"}
    for name, c in (state.CFG.client_auth.get("clients") or {}).items():
        th = c.get("token_sha256") or ""
        out[name] = {"source": "config.yaml" if name in base else "ui", "has_token": bool(th), "token_prefix": th[:8],
                     "token_rotated": (ov.get(name) or {}).get("token_rotated") or c.get("token_rotated"), "created": c.get("created"),
                     "ip": c.get("ip") or [], "roles": c.get("roles") or ["*"], "models": c.get("models", True),
                     "requests_per_minute": c.get("requests_per_minute"), "max_priority": c.get("max_priority"),
                     "cloud": c.get("cloud", True), "data_class": c.get("data_class"), "note": c.get("note"),
                     "privacy_check": c.get("privacy_check") is True}   # config.yaml only (privacy.py), shown read-only
    return out


def _client_spec(name, v):
    """Validate client fields from the UI: return the fields or a Problem 400 with plain text."""
    if not isinstance(v, dict):
        return invalid(f"client {name}: object expected")
    bad = set(v) - set(CLIENT_FIELDS)
    if bad:
        return invalid(f"client {name}: field(s) {sorted(bad)} cannot be set in the UI (secret: POST /admin/clients/{name}/token)")
    spec = {}
    if v.get("ip"):
        ips = v["ip"] if isinstance(v["ip"], list) else [x.strip() for x in str(v["ip"]).replace(";", ",").split(",") if x.strip()]
        spec["ip"] = [values.ip_address(x, f"client {name}") for x in ips]
        problem = next((p for p in spec["ip"] if failed(p)), None)
        if problem:
            return problem
    if v.get("roles"):
        spec["roles"] = [str(x).strip() for x in (v["roles"] if isinstance(v["roles"], list) else str(v["roles"]).split(",")) if str(x).strip()]
    for k in ("models", "cloud"):
        if v.get(k) is not None:
            spec[k] = bool(v[k])
    if v.get("requests_per_minute") is not None:
        spec["requests_per_minute"] = values.number(v["requests_per_minute"], f"client {name}: requests_per_minute", integer=True)
        if failed(spec["requests_per_minute"]):
            return spec["requests_per_minute"]
    for k in ("max_priority", "data_class"):
        if v.get(k):
            spec[k] = str(v[k])
    if v.get("note"):
        spec["note"] = str(v["note"])[:200]
    return spec


async def handle_client_token(request):
    """New secret for a client: Random (256 Bit), only the sha256 lands in roles.yaml; the plaintext goes ONCE to the UI
    and is never stored or logged. The previous secret is immediately invalid (rotation)."""
    name = request.match_info["name"]
    if name not in (state.CFG.client_auth.get("clients") or {}):
        return web.json_response({"error": f"unknown client {name}"}, status=404)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    current = config.read_overrides()
    cl = dict(current.get("clients") or {})
    cl[name] = {**(cl.get(name) or {}), "token_sha256": digest, "token_rotated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    written = config.write_overrides({**current, "clients": cl})
    if failed(written):
        return web.json_response({"error": f"invalid configuration: {written.message}" if written.status == 400 else written.message},
                                 status=written.status)
    auth.audit("client_token_rotated", client=name, by="admin-ui", sha256_prefix=digest[:8])
    state.remember({"event": "config", "client_token": name})
    return web.json_response({"ok": True, "client": name, "token": token, "sha256_prefix": digest[:8],
                              "hint": "Einmalig sichtbar. In den Client eintragen (HA: API-Key im Ollama-Eintrag, Node-RED: apiKey der llm-config) "
                                      "und in Secret-Store ablegen - der Router speichert nur den Hash."})


def _bad(msg):
    return web.json_response({"error": msg}, status=400)


async def handle_decide(request):
    """Query the Decision Engine directly (UI, decision-eval): POST {"context": "...", "options": [..]?, "engine": "rules"?}
    or {"body": <Ollama-Body>, "path": "/api/chat"} - then the Router builds the context as in production."""
    d = decision.active()
    if not d:
        return _bad("decision engine is not active")
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    engines = None
    if b.get("engine"):
        if b["engine"] not in d.engines:
            return _bad(f"unknown engine {b['engine']!r}; configured: {sorted(d.engines)}")
        engines = [d.engines[b["engine"]]]
    if b.get("body"):
        context, meta = decision.context_from(b["body"], b.get("path") or "/api/chat", d.context_chars)
    else:
        context, meta = str(b.get("context") or "")[: d.context_chars], {}
    if not context:
        return _bad("context or body missing")
    options = [str(o) for o in (b.get("options") or d.options)]
    result = await d.decide(DecisionRequest(context=context, options=options, metadata=meta), engines=engines)
    return web.json_response({**result.as_dict(), "context": context})


async def handle_decision_status(request):
    d = decision.active()
    if not d:
        return web.json_response({"enabled": False})
    return web.json_response(await d.status())


async def handle_config_put(request):
    """Roles, model catalog, settings, clients from the UI: validate, write roles.yaml, switch live.
    The body carries only the changed sections; they are merged with existing overrides."""
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    ov = _overrides_from_request(b)
    estimated = ov if failed(ov) else _estimate_unknown_tier_models(ov)
    new = estimated if failed(estimated) else _merge_overrides(config.read_overrides(), ov)
    if failed(new):
        return _bad(new.message)
    written = config.write_overrides(new)   # Schema and validation errors land as 400 for the operator
    if failed(written):
        return _bad(f"invalid configuration: {written.message}") if written.status == 400 else \
            web.json_response({"error": written.message}, status=written.status)
    _after_config_change(b, new)
    return web.json_response({"ok": True, "roles": len(state.CFG.roles), "settings": len(new.get("settings") or {})})


def _overrides_from_request(b):
    """UI body -> override sections (validated, not yet merged with existing state), or a Problem 400."""
    if not isinstance(b, dict):
        return invalid("object expected")
    for key in ("roles", "models", "settings", "clients"):   # Stage 7 settings: {path: value}; null = remove override
        if not isinstance(b.get(key) or {}, dict):
            return invalid(f"{key} must be a mapping")
    for key in ("delete_models", "delete_clients"):
        if not isinstance(b.get(key) or [], list):
            return invalid(f"{key} must be a list")
    ov = {}
    if "roles" in b:
        ov["roles"] = _roles_from(b["roles"] or {})
    if "models" in b:
        ov["models"] = _models_from(b["models"] or {})
    if "settings" in b:
        ov["settings"] = dict(b["settings"] or {})
    if "delete_models" in b:
        ov["delete_models"] = [str(m) for m in (b["delete_models"] or [])]
    if "clients" in b:   # Clients tab: new clients or fields of existing ones; the secret comes separately via /admin/clients/<n>/token
        ov["clients"] = _clients_from(b["clients"] or {})
    if "delete_clients" in b:
        ov["delete_clients"] = [str(n) for n in (b["delete_clients"] or [])]
    if "expose_concrete_models" in b:
        ov["expose_concrete_models"] = bool(b["expose_concrete_models"])
    return next((v for v in ov.values() if failed(v)), ov)


def _tier_from(role, t):
    if not isinstance(t, dict) or not t.get("model"):
        return invalid(f"role {role}: every tier needs model and num_ctx")
    ctx = values.number(t.get("num_ctx"), f"role {role}: num_ctx", integer=True)
    return ctx if failed(ctx) else {"model": str(t["model"]), "num_ctx": ctx, "busy_ok": bool(t.get("busy_ok", False))}


def _roles_from(raw):
    roles = {}
    for name, r in raw.items():
        name = str(name).strip()
        if not name or ":" in name or " " in name:
            return invalid(f"invalid role name {name!r}")
        tiers = (r.get("tiers") if isinstance(r, dict) else None) or []
        if not isinstance(tiers, list) or not tiers:
            return invalid(f"role {name}: at least one tier")
        roles[name] = {"exposed_as": f"{name}:latest", "tiers": [_tier_from(name, t) for t in tiers]}
        problem = next((t for t in roles[name]["tiers"] if failed(t)), None)
        if problem:
            return problem
        if r.get("latency_first") is not None:
            roles[name]["latency_first"] = bool(r["latency_first"])
        for k in ("priority", "canary", "shadow"):   # Stage 3/6: the UI may set them; if missing, config.yaml remains
            if r.get(k) is not None:
                roles[name][k] = r[k]
    return roles


def _models_from(raw):
    models = {}
    for m, v in raw.items():
        m = str(m).strip()
        if not m or " " in m:
            return invalid(f"invalid model name {m!r}")
        if not isinstance(v, dict):
            return invalid(f"model {m}: object expected")
        entry = _cloud_model_entry(m, v) if v.get("cloud") else _local_model_entry(m, v)
        if failed(entry):
            return entry
        if v.get("max_parallel"):   # 0.6.5: Limit of concurrent requests per node for this model
            entry["max_parallel"] = values.number(v["max_parallel"], f"model {m}: max_parallel", integer=True)
            if failed(entry["max_parallel"]):
                return entry["max_parallel"]
            if entry["max_parallel"] < 1:
                return invalid(f"model {m}: max_parallel must be at least 1")
        models[m] = entry
    return models


def _local_model_entry(m, v):
    weights = values.number(v.get("weights_gib"), f"model {m}: weights_gib")
    kv = values.number(v.get("kv_gib_per_1k", 0), f"model {m}: kv_gib_per_1k")
    return next((x for x in (weights, kv) if failed(x)), None) or {"weights_gib": weights, "kv_gib_per_1k": kv}


def _cloud_model_entry(m, v):
    """Stage 7: Create/edit cloud model in catalog (provider, prices, context, capabilities)."""
    if v["cloud"] not in (state.CFG.cloud.get("providers") or {}):
        return invalid(f"model {m}: cloud provider {v['cloud']} is not configured")
    pr = v.get("price_chf_per_m") or {}
    if not isinstance(pr, dict) or not isinstance(v.get("capabilities") or {}, dict):
        return invalid(f"model {m}: price_chf_per_m and capabilities must be mappings")
    prices = {k: values.number(pr.get(k, 0), f"model {m}: price_chf_per_m.{k}") for k in ("input", "output")}
    problem = next((p for p in prices.values() if failed(p)), None)
    if problem:
        return problem
    e = {"cloud": str(v["cloud"]), "price_chf_per_m": prices}
    if v.get("provider_model"):
        e["provider_model"] = str(v["provider_model"]).strip()
    if v.get("context_tokens"):
        e["context_tokens"] = values.number(v["context_tokens"], f"model {m}: context_tokens", integer=True)
        if failed(e["context_tokens"]):
            return e["context_tokens"]
    if v.get("reasoning") is not None:
        e["reasoning"] = bool(v["reasoning"])
    if v.get("capabilities") is not None:
        e["capabilities"] = {str(k): bool(x) for k, x in v["capabilities"].items()}
    if v.get("note"):
        e["note"] = str(v["note"])[:200]
    return e


def _clients_from(raw):
    clients = {}
    for name, v in raw.items():
        name = str(name).strip()
        if not CLIENT_NAME.match(name):
            return invalid(f"invalid client name {name!r} (a-z, 0-9, -, _)")
        clients[name] = _client_spec(name, v or {})
        if failed(clients[name]):
            return clients[name]
    return clients


def _estimate_unknown_tier_models(ov):
    """Tier models without catalog entry: derive from node knowledge (file size), otherwise a Problem 400."""
    known = set(state.CFG.models) | set(ov.get("models", {}))
    for name, r in ov.get("roles", {}).items():
        for t in r["tiers"]:
            if t["model"] in known:
                continue
            size = max((n.model_details.get(t["model"], {}).get("size", 0) for n in state.NODES.values()), default=0)
            if not size:
                return invalid(f"role {name}: model {t['model']} is neither in the catalog nor on a node")
            ov.setdefault("models", {})[t["model"]] = {"weights_gib": round(size / GIB * 1.05, 2), "kv_gib_per_1k": 0.1,
                                                        "source": "estimated", "note": "estimated from the file size, please measure"}
            known.add(t["model"])


def _merge_overrides(current, ov):
    """Merge new sections into existing overrides: supplement catalog and clients, delete settings with null,
    apply deletion lists. Problem 400: model still in roles, client from config.yaml."""
    if "models" in ov:
        cur_models = current.get("models", {})
        merged = dict(cur_models)
        for m, e in ov["models"].items():
            old_entry = cur_models.get(m) or {}
            # The UI sends only weights + KV. If unchanged, the measured entry with real /
            # vram_real_gib (0.3.5) remains - otherwise every save in the catalog tab would lose the actual measurement.
            if (old_entry.get("real") and not e.get("cloud") and old_entry.get("weights_gib") == e.get("weights_gib")
                    and old_entry.get("kv_gib_per_1k") == e.get("kv_gib_per_1k")):
                if "max_parallel" in e:   # 0.6.5: the measurement remains, the limit from the UI still applies
                    merged[m] = {**old_entry, "max_parallel": e["max_parallel"]}
                continue
            merged[m] = e
        ov["models"] = merged
    if "settings" in ov:
        merged = dict(current.get("settings") or {})
        for k, v in ov["settings"].items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        ov["settings"] = merged
    if "delete_models" in ov:
        problem = _apply_delete_models(current, ov)
        if failed(problem):
            return problem
    if "clients" in ov:
        ov["clients"] = _merge_clients(current, ov["clients"])
    if "delete_clients" in ov:
        problem = _apply_delete_clients(current, ov)
        if failed(problem):
            return problem
    new = {**current, **ov}
    for key in ("settings", "clients"):
        if new.get(key) == {}:
            new.pop(key)
    return new


def _apply_delete_models(current, ov):
    base_models = set(state.CFG.base().get("models") or {})
    gone = [m for m in ov.pop("delete_models") if m not in base_models]
    ov["models"] = {m: v for m, v in {**current.get("models", {}), **ov.get("models", {})}.items() if m not in gone}
    used = [(r, t["model"]) for r, rr in {**current.get("roles", {}), **ov.get("roles", {})}.items() for t in rr.get("tiers", []) if t["model"] in gone]
    return invalid(f"model is still used in roles: {used}") if used else None


def _merge_clients(current, incoming):
    merged = dict(current.get("clients") or {})
    base = base_clients()
    for name, spec in incoming.items():
        if name not in base and name not in merged:
            spec = {**spec, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        merged[name] = {**(merged.get(name) or {}), **spec}
    return merged


def _apply_delete_clients(current, ov):
    blocked = [n for n in ov["delete_clients"] if n in base_clients()]
    if blocked:
        return invalid(f"client(s) {blocked} are in config.yaml and can only be removed by deploy")
    gone = set(ov.pop("delete_clients"))
    ov["clients"] = {n: v for n, v in {**(current.get("clients") or {}), **ov.get("clients", {})}.items() if n not in gone}
    ov["settings"] = {k: v for k, v in {**(current.get("settings") or {}), **ov.get("settings", {})}.items()
                      if not any(k.startswith(f"router.client_auth.clients.{n}.") for n in gone)}


def _after_config_change(b, new):
    if "settings" in b and any(k == "router.client_auth.mode" for k in b.get("settings") or {}):
        state.CLIENT_AUTH_MODE = None   # the persistent value applies; an older runtime switch (POST /admin/client_auth) expires
        auth.audit("client_auth_mode", mode=state.CFG.client_auth["mode"], by="admin-ui-settings")
    log.info("config changed via UI: %d roles, expose_concrete=%s, settings=%d", len(state.CFG.roles), state.CFG.expose_concrete, len(new.get("settings") or {}))
    state.remember({"event": "config", "roles": sorted(r["name"] for r in state.CFG.roles.values()),
                    "settings": sorted((b.get("settings") or {}).keys())[:12]})


async def handle_try(request):
    """Send a test prompt through the own proxy and provide the routing decision."""
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    body = {"model": b.get("model"), "stream": False, "messages": [{"role": "user", "content": b.get("prompt") or "Antworte nur mit OK"}]}
    if b.get("num_ctx"):
        ctx = values.number(b["num_ctx"], "num_ctx", integer=True)
        if failed(ctx):
            return _bad(ctx.message)
        body["options"] = {"num_ctx": ctx}
    if "think" in b:
        body["think"] = bool(b["think"])   # thinking models (glm, qwen3.6, gpt-oss, granite) otherwise think invisibly before the answer
    rt = {k: v for k, v in (b.get("routing") or {}).items() if v not in (None, "", [])}   # UI: execution, data_class, priority, require
    rid = f"ui-{int(time.time() * 1000)}"
    body["routing"] = {**rt, "request_id": rid}   # always a block -> routing info in the response
    h, p = split_listen(state.CFG.listen)
    url = f"{'https' if state.CFG.api_tls else 'http'}://127.0.0.1:{p}/api/chat"   # Self-call; certificate is for the hostname -> ssl=False
    t0 = time.time()
    hdrs = {"Authorization": "Bearer " + state.INTERNAL_TOKEN} if state.INTERNAL_TOKEN else {}   # eigene Identitaet skirnir-ui (enforce)
    try:
        async with state.SESSION.post(url, json=body, headers=hdrs, timeout=ClientTimeout(total=state.CFG.request_timeout_s), ssl=False if state.CFG.api_tls else None) as r:
            data = await r.json(content_type=None)
            status = r.status
    except Exception as e:  # noqa: BLE001  # exception-ok: network boundary of the self-call; the operator sees the reason
        return web.json_response({"status": 502, "answer": str(e), "seconds": round(time.time() - t0, 1)})
    # via request_id, not by index: the ring is full after loading events.jsonl, a slice from len() would be empty
    decision = next((d for d in reversed(state.DECISIONS) if d.get("event") == "route" and d.get("request_id") == rid), None)
    answer = data.get("error") if "error" in data else (data.get("message") or {}).get("content", "")
    thinking = (data.get("message") or {}).get("thinking") or "" if isinstance(data, dict) else ""
    return web.json_response({"status": status, "answer": answer, "seconds": round(time.time() - t0, 1),
                              "load_ms": round((data.get("load_duration") or 0) / 1e6), "decision": decision,
                              "backend_ms": round((data.get("total_duration") or 0) / 1e6),   # Ollamas eigene Zeit (Laden + Prompt + Generieren)
                              "queued_ms": (data.get("routing") or {}).get("queued_ms") if isinstance(data, dict) else None,   # Wait time in admission
                              "routing": data.get("routing") if isinstance(data, dict) else None,
                              "perf": perf.perf_stats(data, None) if isinstance(data, dict) else None,
                              "thinking_chars": len(thinking)})


def _decisions_n(request):
    raw = request.query.get("decisions", "50")
    return max(1, min(int(raw), state.DECISIONS_KEEP)) if re.fullmatch(r"\d{1,7}", raw) else 50


LOADTEST = {"running": False}


async def handle_loadtest(request):
    """Load test from the UI (operator, 2026-09-11): n requests with parallelism c through the own proxy, plus optionally every 2 s
    a probe with routing.priority interactive - proof that voice commands (assist) overtake the queue.
    Result: throughput, latency (median/p95/max), wait time normal vs. interactive, model time, node distribution, errors."""
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    if LOADTEST["running"]:
        return web.json_response({"error": "a load test is already running"}, status=409)
    n, conc, ctx = (values.number(b.get(k) or d, k, integer=True) for k, d in (("n", 20), ("concurrency", 4), ("num_ctx", 0)))
    problem = next((p for p in (n, conc, ctx) if failed(p)), None)
    if problem:
        return _bad(problem.message)
    n, conc = max(1, min(n, 300)), max(1, min(conc, 16))
    model = b.get("model") or "standard:latest"
    prompt = b.get("prompt") or "Antworte nur mit OK."
    probe = bool(b.get("probe_interactive"))
    h, p = split_listen(state.CFG.listen)
    url = f"{'https' if state.CFG.api_tls else 'http'}://127.0.0.1:{p}/api/chat"
    hdrs = {"Authorization": "Bearer " + state.INTERNAL_TOKEN} if state.INTERNAL_TOKEN else {}
    results, probes = [], []

    async def one(i, prio, sink):
        body = {"model": model, "stream": False, "think": False, "messages": [{"role": "user", "content": prompt}],
                "routing": {"request_id": f"lt-{prio[0]}{i}-{int(t_start)}", "priority": prio}}
        if ctx:
            body["options"] = {"num_ctx": ctx}
        t0 = time.time()
        try:
            async with state.SESSION.post(url, json=body, headers=hdrs, timeout=ClientTimeout(total=state.CFG.request_timeout_s),
                                          ssl=False if state.CFG.api_tls else None) as r:
                data = await r.json(content_type=None)
                status = r.status
        except Exception as e:  # noqa: BLE001  # exception-ok: network boundary of the self-call; counted as a failed request
            sink.append({"wall": time.time() - t0, "status": 599, "error": str(e)[:80]})
            return
        ri = (data.get("routing") or {}) if isinstance(data, dict) else {}
        sink.append({"wall": time.time() - t0, "status": status, "queued": (ri.get("queued_ms") or 0) / 1000,
                     "model_s": (data.get("total_duration") or 0) / 1e9 if isinstance(data, dict) else 0, "node": ri.get("node"),
                     "error": data.get("error") if isinstance(data, dict) and status != 200 else None})

    async def worker(q):
        while q:
            i = q.pop()
            await one(i, "normal", results)

    async def prober():
        k = 0
        while len(results) < n:
            await asyncio.sleep(2)
            if len(results) >= n:
                break
            k += 1
            await one(k, "interactive", probes)

    LOADTEST["running"] = True
    t_start = time.time()
    try:
        queue = list(range(n))[::-1]
        tasks = [asyncio.create_task(worker(queue)) for _ in range(conc)]
        ptask = asyncio.create_task(prober()) if probe else None
        await asyncio.gather(*tasks)
        if ptask:
            ptask.cancel()
            await asyncio.gather(ptask, return_exceptions=True)   # wait for the cancelled probe; its outcome does not matter
    finally:
        LOADTEST["running"] = False
    total = time.time() - t_start

    def stats(rows):
        ok = [r for r in rows if r["status"] == 200]
        walls = sorted(r["wall"] for r in ok)
        pct = lambda q: walls[min(len(walls) - 1, int(q * len(walls)))] if walls else None  # noqa: E731
        nodes = {}
        for r in ok:
            nodes[r.get("node") or "?"] = nodes.get(r.get("node") or "?", 0) + 1
        return {"count": len(rows), "ok": len(ok), "errors": [r.get("error") for r in rows if r["status"] != 200][:5],
                "wall_p50": pct(0.5), "wall_p95": pct(0.95), "wall_max": walls[-1] if walls else None,
                "queue_mean": sum(r["queued"] for r in ok) / len(ok) if ok else None, "queue_max": max((r["queued"] for r in ok), default=None),
                "model_mean": sum(r["model_s"] for r in ok) / len(ok) if ok else None, "nodes": nodes}
    out = {"n": n, "concurrency": conc, "model": model, "seconds": round(total, 2), "throughput": round(len(results) / total, 2) if total else None,
           "normal": stats(results), "interactive": stats(probes) if probe else None}
    state.remember({"event": "loadtest", "n": n, "concurrency": conc, "seconds": round(total, 1), "throughput": out["throughput"], "client": state.INTERNAL_CLIENT})
    return web.json_response(out)


# Catalog sources written in German in roles.yaml before Router 0.9.0 (design/english-names.md)
CATALOG_SOURCE_BEFORE_0_9 = {"gemessen": "measured", "gemessen (nvidia-smi)": "measured (nvidia-smi)", "geschaetzt": "estimated"}


def catalog_effective():
    """Effective catalog values for each known model: direct, via digest twin (alias), or none."""
    out = {}
    names = set(state.CFG.models)
    for n in state.NODES.values():
        names |= n.models
    for m in sorted(names):
        if m in state.CFG.models:
            e = state.CFG.models[m]
            src = e.get("source") or ("estimated" if e.get("note") else "measured")
            src = CATALOG_SOURCE_BEFORE_0_9.get(src, src)
            out[m] = {**e, "source": src}
            continue
        twin = None
        for n in state.NODES.values():
            d = n.digest_of.get(m)
            if d:
                twin = next((cm for cm in state.CFG.models if n.digest_of.get(cm) == d), None)
                if twin:
                    break
        out[m] = {**state.CFG.models[twin], "source": f"Alias von {twin}"} if twin else {"source": None}
    return out


async def handle_state(request):
    now = time.time()
    return web.json_response({
        "version": VERSION,
        "public_url": state.CFG.public_url,
        "measuring": state.MEASURING,
        "metrics_since": state.METRICS.get("since"),   # Start of metrics.json recording (UI: "total since")
        "benching": state.BENCHING,
        "perf": state.PERF,
        "fit_overhead_gib": state.CFG.overhead_gib,
        "catalog_effective": catalog_effective(),
        "nodes": {n.name: n.snapshot(now) for n in state.NODES.values()},
        "roles": {e: {"tiers": r["tiers"], "latency_first": r["latency_first"], "priority": r.get("priority", "normal")} for e, r in state.CFG.roles.items()},
        "usage_today": metrics.usage_totals(metrics.usage_today()),
        "capabilities": {k: v for k, v in state.CAPS.items() if v is not None},
        "capabilities_effective": {m: request_mod.effective_caps(m) for m in sorted(set(state.CAPS) | set(state.CFG.models))
                                   if request_mod.effective_caps(m) is not None},
        "limits": state.CFG.limits,
        "sessions": len(state.SESSIONS),
        "admission": admission.view(),
        "idempotency": ops.idem_view(),
        "cloud": cloud.view(),
        "cloud_config": {**{k: v for k, v in state.CFG.cloud.items() if k not in ("providers", "privacy_check")},
                         "privacy_check": _privacy_view(state.CFG.cloud.get("privacy_check"))},
        "build": ops.build_info(),
        "supply_chain": ops.supply_chain(),
        "scheduler": {"score": state.CFG.score, "breaker": state.CFG.breaker, "admission": state.CFG.admission},
        "decisions": state.DECISIONS[-_decisions_n(request):],   # ?decisions=N (1..DECISIONS_KEEP), Standard 50
        "client_auth": auth.client_auth_view(),
    })


async def handle_usage(request):
    return web.json_response(metrics.usage_view())


async def handle_metrics(request):
    return web.Response(text=metrics.render(), content_type="text/plain", charset="utf-8")


async def handle_client_auth(request):
    """GET: View of the observation phase. POST {"mode": "observe"|"enforce"}: Switch mode at runtime -
    applies until restart; persistent is config.yaml. Intended for switching after observation and as
    a brake back to observe without deploy."""
    if request.method == "POST":
        b = await read_json(request)
        if b is None:
            return _bad("invalid json")
        mode = b.get("mode")
        if mode not in ("observe", "enforce"):
            return web.json_response({"error": "mode must be observe or enforce"}, status=400)
        if state.CFG.client_auth.get("locked"):
            auth.audit("client_auth_mode_rejected", mode=mode, by="admin-api", reason="locked")
            return web.json_response({"error": "client_auth.mode is locked (client_auth.locked in config.yaml), change it via deploy"}, status=403)
        state.CLIENT_AUTH_MODE = mode
        auth.audit("client_auth_mode", mode=mode, by="admin-api")
        log.warning("client_auth: mode set to %s at runtime (permanent only via config.yaml)", mode)
    elif request.method != "GET":
        return web.json_response({"error": "method not allowed"}, status=405)
    return web.json_response(auth.client_auth_view())
