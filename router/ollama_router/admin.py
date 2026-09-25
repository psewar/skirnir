"""Control-Plane: UI, Zustand, Konfiguration, Probelauf."""

import asyncio
import hashlib
import ipaddress
import os
import re
import secrets
import time

from aiohttp import ClientTimeout, web

from . import auth, config, perf, state
from . import admission, cloud, decision, metrics, ops
from .decision.base import DecisionRequest
from . import request as request_mod
from .common import VERSION, GIB, log, read_json, split_listen


UI_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.html")   # ui.html liegt neben router.py, eine Ebene ueber dem Paket


async def handle_ui(request):
    if not os.path.exists(UI_PATH):
        return web.Response(text="ui.html fehlt", status=404)
    return web.FileResponse(UI_PATH, headers={"Cache-Control": "no-store"})


ASSETS = {"skirnir.png": "skirnir.png", "favicon.png": "favicon.png", "favicon.ico": "favicon.png"}   # Logo des Betreibers (Kachel seit 2026-09-16; vorher Rabe) freigestellt + Icon mit Kachel


async def handle_asset(request):
    """Logo (Skirnir-Kachel, 256 px, Ecken transparent) und Tab-Icon (128 px); /favicon.ico liefert dasselbe PNG."""
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
                              "settings": config.settings_view(),                     # Stufe 7: per UI aenderbare Einstellungen
                              "ui_models": sorted(m for m in state.CFG.models if m not in base_models),   # Katalogeintraege aus roles.yaml (loeschbar)
                              "cloud_providers": sorted(state.CFG.cloud.get("providers") or {}),
                              "clients": clients_view(),                                 # Clients-Tab: Identitaet, Herkunft, Felder
                              "overrides_file": state.CFG.overrides_path, "overrides_active": os.path.exists(state.CFG.overrides_path),
                              "overrides_settings": ov.get("settings") or {}})


CLIENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
CLIENT_FIELDS = ("ip", "roles", "models", "requests_per_minute", "max_priority", "cloud", "data_class", "note")


def base_clients():
    return set(((state.CFG.base().get("router") or {}).get("client_auth") or {}).get("clients") or {})


def clients_view():
    """Alle wirksamen Clients mit Herkunft (config.yaml / UI) und Identitaetsmerkmalen - nie der Hash selbst in voller Laenge."""
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
                     "cloud": c.get("cloud", True), "data_class": c.get("data_class"), "note": c.get("note")}
    return out


def _client_spec(name, v):
    """Felder eines Clients aus der UI pruefen (ValueError mit Klartext)."""
    if not isinstance(v, dict):
        raise ValueError(f"Client {name}: Objekt erwartet")
    bad = set(v) - set(CLIENT_FIELDS)
    if bad:
        raise ValueError(f"Client {name}: Feld(er) {sorted(bad)} nicht ueber die UI setzbar (Secret: POST /admin/clients/{name}/token)")
    spec = {}
    if v.get("ip"):
        ips = v["ip"] if isinstance(v["ip"], list) else [x.strip() for x in str(v["ip"]).replace(";", ",").split(",") if x.strip()]
        spec["ip"] = [str(ipaddress.ip_address(str(x).strip())) for x in ips]
    if v.get("roles"):
        spec["roles"] = [str(x).strip() for x in (v["roles"] if isinstance(v["roles"], list) else str(v["roles"]).split(",")) if str(x).strip()]
    for k in ("models", "cloud"):
        if v.get(k) is not None:
            spec[k] = bool(v[k])
    if v.get("requests_per_minute") is not None:
        spec["requests_per_minute"] = int(v["requests_per_minute"])
    for k in ("max_priority", "data_class"):
        if v.get(k):
            spec[k] = str(v[k])
    if v.get("note"):
        spec["note"] = str(v["note"])[:200]
    return spec


async def handle_client_token(request):
    """Neues Secret fuer einen Client: Zufall (256 Bit), nur der sha256 landet in roles.yaml; der Klartext geht EINMAL an die UI
    und wird nirgends gespeichert oder geloggt. Das bisherige Secret ist sofort ungueltig (Rotation)."""
    name = request.match_info["name"]
    if name not in (state.CFG.client_auth.get("clients") or {}):
        return web.json_response({"error": f"Client {name} unbekannt"}, status=404)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    current = config.read_overrides()
    cl = dict(current.get("clients") or {})
    cl[name] = {**(cl.get(name) or {}), "token_sha256": digest, "token_rotated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        config.write_overrides({**current, "clients": cl})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": f"Konfiguration ungültig: {e}"}, status=400)
    auth.audit("client_token_rotated", client=name, by="admin-ui", sha256_prefix=digest[:8])
    state.remember({"event": "config", "client_token": name})
    return web.json_response({"ok": True, "client": name, "token": token, "sha256_prefix": digest[:8],
                              "hint": "Einmalig sichtbar. In den Client eintragen (HA: API-Key im Ollama-Eintrag, Node-RED: apiKey der llm-config) "
                                      "und in Secret-Store ablegen - der Router speichert nur den Hash."})


def _bad(msg):
    return web.json_response({"error": msg}, status=400)


async def handle_decide(request):
    """Decision Engine direkt fragen (UI, decision-eval): POST {"context": "...", "options": [..]?, "engine": "rules"?}
    oder {"body": <Ollama-Body>, "path": "/api/chat"} - dann baut der Router den Kontext wie im Betrieb."""
    d = decision.active()
    if not d:
        return _bad("decision_engine ist nicht aktiv")
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    engines = None
    if b.get("engine"):
        if b["engine"] not in d.engines:
            return _bad(f"unbekannte Engine {b['engine']!r}; konfiguriert: {sorted(d.engines)}")
        engines = [d.engines[b["engine"]]]
    if b.get("body"):
        context, meta = decision.context_from(b["body"], b.get("path") or "/api/chat", d.context_chars)
    else:
        context, meta = str(b.get("context") or "")[: d.context_chars], {}
    if not context:
        return _bad("context oder body fehlt")
    options = [str(o) for o in (b.get("options") or d.options)]
    result = await d.decide(DecisionRequest(context=context, options=options, metadata=meta), engines=engines)
    return web.json_response({**result.as_dict(), "context": context})


async def handle_decision_status(request):
    d = decision.active()
    if not d:
        return web.json_response({"enabled": False})
    return web.json_response(await d.status())


async def handle_config_put(request):
    """Rollen, Modellkatalog, Einstellungen, Clients von der UI: validieren, roles.yaml schreiben, live schalten.
    Der Body traegt nur die geaenderten Abschnitte; sie werden mit den bestehenden Overrides zusammengefuehrt."""
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    try:
        ov = _overrides_from_request(b)
        _estimate_unknown_tier_models(ov)
        new = _merge_overrides(config.read_overrides(), ov)
    except (ValueError, TypeError) as e:
        return _bad(str(e))
    try:
        config.write_overrides(new)
    except Exception as e:  # noqa: BLE001 - Schema- und YAML-Fehler landen als 400 beim Bediener
        return _bad(f"Konfiguration ungültig: {e}")
    _after_config_change(b, new)
    return web.json_response({"ok": True, "roles": len(state.CFG.roles), "settings": len(new.get("settings") or {})})


def _overrides_from_request(b):
    """Body der UI -> Override-Abschnitte (validiert, noch nicht mit dem Bestand zusammengefuehrt). ValueError = 400."""
    ov = {}
    if "roles" in b:
        ov["roles"] = _roles_from(b["roles"] or {})
    if "models" in b:
        ov["models"] = _models_from(b["models"] or {})
    if "settings" in b:   # Stufe 7: {pfad: wert}; null = Override entfernen (zurueck auf config.yaml)
        if not isinstance(b["settings"], dict):
            raise ValueError("settings muss ein Mapping Pfad -> Wert sein")
        ov["settings"] = dict(b["settings"])
    if "delete_models" in b:
        ov["delete_models"] = [str(m) for m in (b["delete_models"] or [])]
    if "clients" in b:   # Clients-Tab: neue Clients oder Felder bestehender; das Secret kommt separat ueber /admin/clients/<n>/token
        if not isinstance(b["clients"], dict):
            raise ValueError("clients muss ein Mapping Name -> Felder sein")
        ov["clients"] = _clients_from(b["clients"])
    if "delete_clients" in b:
        ov["delete_clients"] = [str(n) for n in (b["delete_clients"] or [])]
    if "expose_concrete_models" in b:
        ov["expose_concrete_models"] = bool(b["expose_concrete_models"])
    return ov


def _roles_from(raw):
    roles = {}
    for name, r in raw.items():
        name = str(name).strip()
        if not name or ":" in name or " " in name:
            raise ValueError(f"ungültiger Rollenname {name!r}")
        tiers = r.get("tiers") or []
        if not tiers:
            raise ValueError(f"Rolle {name}: mindestens ein Tier")
        roles[name] = {"exposed_as": f"{name}:latest",
                       "tiers": [{"model": str(t["model"]), "num_ctx": int(t["num_ctx"]), "busy_ok": bool(t.get("busy_ok", False))} for t in tiers]}
        if r.get("latency_first") is not None:
            roles[name]["latency_first"] = bool(r["latency_first"])
        for k in ("priority", "canary", "shadow"):   # Stufe 3/6: die UI darf sie setzen; fehlen sie, bleibt config.yaml
            if r.get(k) is not None:
                roles[name][k] = r[k]
    return roles


def _models_from(raw):
    models = {}
    for m, v in raw.items():
        m = str(m).strip()
        if not m or " " in m:
            raise ValueError(f"ungültiger Modellname {m!r}")
        models[m] = _cloud_model_entry(m, v) if v.get("cloud") else {"weights_gib": float(v["weights_gib"]), "kv_gib_per_1k": float(v.get("kv_gib_per_1k", 0))}
    return models


def _cloud_model_entry(m, v):
    """Stufe 7: Cloud-Modell im Katalog anlegen/aendern (Anbieter, Preise, Kontext, Faehigkeiten)."""
    if v["cloud"] not in (state.CFG.cloud.get("providers") or {}):
        raise ValueError(f"Modell {m}: Cloud-Anbieter {v['cloud']} ist nicht konfiguriert")
    pr = v.get("price_chf_per_m") or {}
    try:
        e = {"cloud": str(v["cloud"]), "price_chf_per_m": {"input": float(pr.get("input", 0)), "output": float(pr.get("output", 0))}}
        if v.get("provider_model"):
            e["provider_model"] = str(v["provider_model"]).strip()
        if v.get("context_tokens"):
            e["context_tokens"] = int(v["context_tokens"])
        if v.get("reasoning") is not None:
            e["reasoning"] = bool(v["reasoning"])
        if v.get("capabilities") is not None:
            e["capabilities"] = {str(k): bool(x) for k, x in dict(v["capabilities"]).items()}
        if v.get("note"):
            e["note"] = str(v["note"])[:200]
    except (TypeError, ValueError) as ex:
        raise ValueError(f"Modell {m}: {ex}") from None
    return e


def _clients_from(raw):
    clients = {}
    for name, v in raw.items():
        name = str(name).strip()
        if not CLIENT_NAME.match(name):
            raise ValueError(f"ungültiger Client-Name {name!r} (a-z, 0-9, -, _)")
        clients[name] = _client_spec(name, v or {})
    return clients


def _estimate_unknown_tier_models(ov):
    """Tier-Modelle ohne Katalogeintrag: aus Knotenwissen ableiten (Dateigroesse), sonst ablehnen."""
    known = set(state.CFG.models) | set(ov.get("models", {}))
    for name, r in ov.get("roles", {}).items():
        for t in r["tiers"]:
            if t["model"] in known:
                continue
            size = max((n.model_details.get(t["model"], {}).get("size", 0) for n in state.NODES.values()), default=0)
            if not size:
                raise ValueError(f"Rolle {name}: Modell {t['model']} ist weder im Katalog noch auf einem Knoten")
            ov.setdefault("models", {})[t["model"]] = {"weights_gib": round(size / GIB * 1.05, 2), "kv_gib_per_1k": 0.1,
                                                        "source": "geschaetzt", "note": "geschaetzt aus Dateigroesse, bitte messen"}
            known.add(t["model"])


def _merge_overrides(current, ov):
    """Neue Abschnitte in die bestehenden Overrides einarbeiten: Katalog und Clients ergaenzen, Settings mit null loeschen,
    Loeschlisten anwenden. ValueError = 400 (Modell noch in Rollen, Client aus config.yaml)."""
    if "models" in ov:
        ov["models"] = {**current.get("models", {}), **ov["models"]}
    if "settings" in ov:
        merged = dict(current.get("settings") or {})
        for k, v in ov["settings"].items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        ov["settings"] = merged
    if "delete_models" in ov:
        _apply_delete_models(current, ov)
    if "clients" in ov:
        ov["clients"] = _merge_clients(current, ov["clients"])
    if "delete_clients" in ov:
        _apply_delete_clients(current, ov)
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
    if used:
        raise ValueError(f"Modell wird noch in Rollen benutzt: {used}")


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
        raise ValueError(f"Client(s) {blocked} stehen in config.yaml und lassen sich nur per Deploy entfernen")
    gone = set(ov.pop("delete_clients"))
    ov["clients"] = {n: v for n, v in {**(current.get("clients") or {}), **ov.get("clients", {})}.items() if n not in gone}
    ov["settings"] = {k: v for k, v in {**(current.get("settings") or {}), **ov.get("settings", {})}.items()
                      if not any(k.startswith(f"router.client_auth.clients.{n}.") for n in gone)}


def _after_config_change(b, new):
    if "settings" in b and any(k == "router.client_auth.mode" for k in b.get("settings") or {}):
        state.CLIENT_AUTH_MODE = None   # der dauerhafte Wert gilt; ein aelterer Laufzeit-Schalter (POST /admin/client_auth) verfaellt
        auth.audit("client_auth_mode", mode=state.CFG.client_auth["mode"], by="admin-ui-settings")
    log.info("config via UI geaendert: %d Rollen, expose_concrete=%s, settings=%d", len(state.CFG.roles), state.CFG.expose_concrete, len(new.get("settings") or {}))
    state.remember({"event": "config", "roles": sorted(r["name"] for r in state.CFG.roles.values()),
                    "settings": sorted((b.get("settings") or {}).keys())[:12]})


async def handle_try(request):
    """Testprompt durch den eigenen Proxy schicken und die Routing-Entscheidung dazu liefern."""
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    body = {"model": b.get("model"), "stream": False, "messages": [{"role": "user", "content": b.get("prompt") or "Antworte nur mit OK"}]}
    if b.get("num_ctx"):
        body["options"] = {"num_ctx": int(b["num_ctx"])}
    if "think" in b:
        body["think"] = bool(b["think"])   # thinking-Modelle (glm, qwen3.6, gpt-oss, granite) denken sonst unsichtbar vor der Antwort
    rt = {k: v for k, v in (b.get("routing") or {}).items() if v not in (None, "", [])}   # UI: execution, data_class, priority, require
    body["routing"] = {**rt, "request_id": f"ui-{int(time.time())}"}   # immer ein Block -> Routing-Info in der Antwort
    h, p = split_listen(state.CFG.listen)
    url = f"{'https' if state.CFG.api_tls else 'http'}://127.0.0.1:{p}/api/chat"   # Selbstaufruf; Zertifikat lautet auf den Hostnamen -> ssl=False
    n_before = len(state.DECISIONS)
    t0 = time.time()
    hdrs = {"Authorization": "Bearer " + state.INTERNAL_TOKEN} if state.INTERNAL_TOKEN else {}   # eigene Identitaet skirnir-ui (enforce)
    try:
        async with state.SESSION.post(url, json=body, headers=hdrs, timeout=ClientTimeout(total=state.CFG.request_timeout_s), ssl=False if state.CFG.api_tls else None) as r:
            data = await r.json(content_type=None)
            status = r.status
    except Exception as e:  # noqa: BLE001
        return web.json_response({"status": 502, "answer": str(e), "seconds": round(time.time() - t0, 1)})
    decision = next((d for d in state.DECISIONS[n_before:] if d["event"] == "route"), None)
    answer = data.get("error") if "error" in data else (data.get("message") or {}).get("content", "")
    thinking = (data.get("message") or {}).get("thinking") or "" if isinstance(data, dict) else ""
    return web.json_response({"status": status, "answer": answer, "seconds": round(time.time() - t0, 1),
                              "load_ms": round((data.get("load_duration") or 0) / 1e6), "decision": decision,
                              "backend_ms": round((data.get("total_duration") or 0) / 1e6),   # Ollamas eigene Zeit (Laden + Prompt + Generieren)
                              "queued_ms": (data.get("routing") or {}).get("queued_ms") if isinstance(data, dict) else None,   # Wartezeit in der Admission
                              "routing": data.get("routing") if isinstance(data, dict) else None,
                              "perf": perf.perf_stats(data, None) if isinstance(data, dict) else None,
                              "thinking_chars": len(thinking)})


def _decisions_n(request):
    try:
        return max(1, min(int(request.query.get("decisions", 50)), state.DECISIONS_KEEP))
    except (TypeError, ValueError):
        return 50


LOADTEST = {"running": False}


async def handle_loadtest(request):
    """Lasttest aus der UI (Betreiber, 2026-09-11): n Anfragen mit Parallelitaet c durch den eigenen Proxy, dazu optional alle 2 s
    eine Sonde mit routing.priority interactive - der Nachweis, dass Sprachbefehle (assist) den Stau ueberholen.
    Ergebnis: Durchsatz, Latenz (Median/p95/max), Wartezeit normal vs. interactive, Modellzeit, Knotenverteilung, Fehler."""
    b = await read_json(request)
    if b is None:
        return _bad("invalid json")
    if LOADTEST["running"]:
        return web.json_response({"error": "es laeuft schon ein Lasttest"}, status=409)
    n = max(1, min(int(b.get("n") or 20), 300))
    conc = max(1, min(int(b.get("concurrency") or 4), 16))
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
        if b.get("num_ctx"):
            body["options"] = {"num_ctx": int(b["num_ctx"])}
        t0 = time.time()
        try:
            async with state.SESSION.post(url, json=body, headers=hdrs, timeout=ClientTimeout(total=state.CFG.request_timeout_s),
                                          ssl=False if state.CFG.api_tls else None) as r:
                data = await r.json(content_type=None)
                status = r.status
        except Exception as e:  # noqa: BLE001
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
            try:
                await ptask
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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


def catalog_effective():
    """Für jedes bekannte Modell die wirksamen Katalogwerte: direkt, über Digest-Zwilling (Alias) oder keine."""
    out = {}
    names = set(state.CFG.models)
    for n in state.NODES.values():
        names |= n.models
    for m in sorted(names):
        if m in state.CFG.models:
            e = state.CFG.models[m]
            src = e.get("source") or ("geschaetzt" if e.get("note") else "gemessen")
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
        "cloud_config": {k: v for k, v in state.CFG.cloud.items() if k != "providers"},
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
    """GET: Sicht der Beobachtungsphase. POST {"mode": "observe"|"enforce"}: Modus zur Laufzeit umschalten -
    gilt bis zum Neustart; dauerhaft ist config.yaml. Gedacht fuer den Wechsel nach der Beobachtung und als
    Notbremse zurueck auf observe ohne Deploy."""
    if request.method == "POST":
        b = await read_json(request)
        if b is None:
            return _bad("invalid json")
        mode = b.get("mode")
        if mode not in ("observe", "enforce"):
            return web.json_response({"error": "mode muss observe oder enforce sein"}, status=400)
        if state.CFG.client_auth.get("locked"):
            auth.audit("client_auth_mode_rejected", mode=mode, by="admin-api", reason="locked")
            return web.json_response({"error": "client_auth.mode ist gesperrt (client_auth.locked in config.yaml) - nur per Deploy aenderbar"}, status=403)
        state.CLIENT_AUTH_MODE = mode
        auth.audit("client_auth_mode", mode=mode, by="admin-api")
        log.warning("client_auth: Modus zur Laufzeit auf %s gesetzt (dauerhaft nur ueber config.yaml)", mode)
    elif request.method != "GET":
        return web.json_response({"error": "method not allowed"}, status=405)
    return web.json_response(auth.client_auth_view())
