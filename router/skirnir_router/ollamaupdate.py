"""Ollama-Update ueber den Router (Router 0.3.0, Agent 0.12.0, 2026-09-26). Seit Ollama auf den Knoten als Kind des Agent-
Dienstes laeuft, greift der Updater der Tray-App nicht mehr (laeuft unter dem Benutzer, Pro-Benutzer-Installer). Der Router
uebernimmt die Rolle des Updaters: er kennt die neueste Version und weiss, wann ein Knoten frei ist.

Ablauf
  1. Versionspruefung: alle `check_interval_h` Stunden GET `release_url` (GitHub-API, neuestes Release von ollama/ollama),
     dazu `sha256sum.txt` des Releases -> LATEST = {version, files: {"windows/amd64": {name, url, size, sha256}, ...}},
     gesichert in <config-dir>/ollama-latest.json (ueberlebt einen Neustart).
  2. Auftrag (UI-Knopf oder Rollout-Schleife): Knoten freigegeben, verbunden, `free`, nichts inflight. Durch den Tunnel geht
     {"t": "ollama-update", version, file, url, sha256, size}. Der Agent laedt das Archiv SELBST von seiner festen Quelle
     (GitHub) und vergleicht den Hash mit der dortigen sha256sum.txt - der Router kann nur die Version waehlen, nicht den
     Code (anders als beim Agent-Update gibt es keinen Betreiber-Schluessel, dafuer die feste Quelle im Agenten).
  3. Stand kommt im Heartbeat (`ollama_update: {state, version, message}`): checking, downloading (mit Prozent), extracting,
     swapping, applied, failed. Meldet der Heartbeat (`ollama_version`) oder der Poll (/api/version) die Zielversion, ist
     der Auftrag `done`. Waehrend des Auftrags bekommt der Knoten keine neuen Anfragen (Drain). Fehler oder Stille -> HA-Problem.
  4. Rollout: Policy `ollama_auto_update` je Knoten, nur im Nachtfenster (`window_start`..`window_end`, Ortszeit), Kanarien-
     vogel zuerst (`canary`, sonst der des Agent-Updates), die anderen erst nach `canary_clean_h` Stunden sauberem Betrieb.
"""
import asyncio
import json
import os
import re
import time

import aiohttp
from aiohttp import web

from . import state
from .common import VERSION, log

ASSETS = {("windows", "amd64"): "ollama-windows-amd64.zip", ("windows", "arm64"): "ollama-windows-arm64.zip",
          ("linux", "amd64"): "ollama-linux-amd64.tar.zst", ("linux", "arm64"): "ollama-linux-arm64.tar.zst"}
LATEST = {"version": None, "checked": 0.0, "checked_ts": None, "files": {}, "error": None, "source": None}
RUNNING = ("requested", "checking", "downloading", "extracting", "swapping", "applied")
DRAIN_S = 1800     # laengste Zeit vom Auftrag bis zum Tausch (1,4 GB Download) - so lange keine neuen Anfragen an den Knoten
STALL_S = 3600     # ohne neuen Stand im Heartbeat gilt der Auftrag als haengend
RETRY_S = 6 * 3600 # nach failed/stalled erst nach 6 h wieder automatisch probieren
AGENT_FRESH_S = 600  # vom Agenten gemeldete Ollama-Version hat 10 min Vorrang vor dem Poll (/api/version alle 5 min)


def _ts(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now or time.time()))


def _cfg():
    return state.CFG.ollama_update or {}


def _vt(v):
    """Versions-Tupel zum Vergleichen: '0.34.4' -> (0, 34, 4); Vor-Release-Anhaengsel zaehlen nicht."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "").split("-")[0].split("+")[0]))


def latest_path():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "ollama-latest.json")


def load_latest():
    try:
        with open(latest_path(), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") and isinstance(d.get("files"), dict):
            LATEST.update({k: d[k] for k in ("version", "checked", "checked_ts", "files", "source") if k in d})
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("ollama-latest.json unlesbar: %s", e)


def save_latest():
    try:
        tmp = latest_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(LATEST, f, indent=1)
        os.replace(tmp, latest_path())
    except OSError as e:
        log.warning("ollama-latest.json schreiben: %s", e)


def parse_checksums(txt):
    """Zeilen '<hex>  ./<datei>' (auch '*<datei>' oder ohne Praefix) -> {datei: hex}."""
    out = {}
    for line in txt.splitlines():
        f = line.split()
        if len(f) == 2 and len(f[0]) == 64:
            name = f[1].lstrip("*")
            out[name[2:] if name.startswith("./") else name] = f[0].lower()
    return out


async def check_latest():
    """Neueste Version bei der Quelle erfragen (GitHub-API: tag_name, assets) samt sha256sum.txt; Ergebnis in LATEST."""
    url = _cfg().get("release_url") or ""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"skirnir-router/{VERSION}"}
    try:
        if not url:
            raise ValueError("modes.ollama_update.release_url ist leer")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    raise ValueError(f"HTTP {r.status} von {url}")
                rel = await r.json(content_type=None)
            tag = str(rel.get("tag_name") or rel.get("name") or "")
            version = tag[1:] if tag.startswith("v") else tag
            if not re.match(r"^\d+(\.\d+){1,3}", version):
                raise ValueError(f"tag_name {tag!r} ist keine Version")
            assets = {a["name"]: a for a in rel.get("assets") or [] if a.get("name") and a.get("browser_download_url")}
            sums = assets.get("sha256sum.txt")
            if not sums:
                raise ValueError("Release ohne sha256sum.txt")
            async with s.get(sums["browser_download_url"], headers={"User-Agent": headers["User-Agent"]}) as r:
                if r.status != 200:
                    raise ValueError(f"sha256sum.txt: HTTP {r.status}")
                checks = parse_checksums(await r.text())
        files = {}
        for (osn, arch), name in ASSETS.items():
            a = assets.get(name)
            if a and name in checks:
                files[f"{osn}/{arch}"] = {"name": name, "url": a["browser_download_url"], "size": int(a.get("size") or 0), "sha256": checks[name]}
        if not files:
            raise ValueError("kein bekanntes Archiv im Release")
        changed = version != LATEST["version"]
        LATEST.update(version=version, files=files, error=None, source=url, checked=time.time(), checked_ts=_ts())
        save_latest()
        if changed:
            log.info("ollama-update: neueste Version %s (%s)", version, ", ".join(sorted(files)))
            state.MQTT_DIRTY.append(True)
    except Exception as e:  # noqa: BLE001
        LATEST.update(error=f"{e.__class__.__name__}: {e}"[:200], checked=time.time(), checked_ts=_ts())
        log.warning("ollama-update: Versionspruefung: %s", LATEST["error"])
    return LATEST


def available_for(e):
    """(neueste Version, Archiv) fuer OS/Arch des Knotens, oder (None, None)."""
    facts = e.get("facts") or {}
    f = LATEST["files"].get(f"{facts.get('os')}/{facts.get('arch')}")
    return (LATEST["version"], f) if f else (None, None)


def current_version(e):
    """Zuletzt beobachtete Ollama-Version des Knotens (Register; Heartbeat/Poll pflegen sie), sonst aus der Anmeldung."""
    return e.get("ollama_version") or (e.get("facts") or {}).get("ollama_version") or None


def update_view(e, now=None):
    """Fuer /admin/nodes, UI und HA: laufende Version, neueste Version, Auftragsstand."""
    now = now or time.time()
    cur = current_version(e)
    avail, _ = available_for(e)
    u = dict(e.get("ollama_update") or {})
    if u.get("state") in RUNNING and now - (u.get("t") or 0) > STALL_S:
        u["state"], u["message"] = "stalled", f"no report for {int((now - u['t']) // 60)} min"
    return {"current": cur, "available": avail, "pending": bool(avail and cur and _vt(avail) > _vt(cur)),
            "auto": bool((e.get("policy") or {}).get("ollama_auto_update")), "version_since": e.get("ollama_version_since"), **u}


def note_version(node, version, now=None, source="poll"):
    """Beobachtete Ollama-Version eines Knotens (Poll /api/version oder Heartbeat des Agenten): Wechsel festhalten, offenen
    Auftrag abschliessen. Die Agent-Meldung hat 10 min Vorrang vor dem Poll (der Poll sieht die neue Version erst spaeter)."""
    if not version or version == "?" or state.REG is None or not node.fp:
        return
    now = now or time.time()
    if source == "agent":
        node.ollama_version_agent_ts = now
    elif now - getattr(node, "ollama_version_agent_ts", 0.0) < AGENT_FRESH_S:
        return   # Agent hat gerade gemeldet: der Poll ist die aeltere Sicht
    node.ollama_version = version
    e = state.REG.nodes.get(node.fp)
    if e is None:
        return
    if e.get("ollama_version") != version:
        e["ollama_version"], e["ollama_version_since"] = version, _ts(now)
        state.REG.save()
        log.info("node %s: Ollama %s (%s)", node.name, version, source)
        state.MQTT_DIRTY.append(True)
    u = e.get("ollama_update") or {}
    if u.get("version") and u.get("state") not in ("done", "failed") and u["version"] == version:
        u.update(state="done", message=f"laeuft mit {version}", t=now, ts=_ts(now))
        e["ollama_update"] = u
        state.REG.save()
        state.remember({"event": "ollama_update", "node": e["name"], "state": "done", "version": version})
        state.MQTT_DIRTY.append(True)
        node.draining_until = 0.0
        log.info("node %s: Ollama-Update auf %s abgeschlossen", node.name, version)


def note_hello(e, facts):
    """Anmeldung eines Agenten: Ollama-Version aus den Fakten uebernehmen, falls das Register noch keine kennt."""
    v = (facts or {}).get("ollama_version")
    if v and not e.get("ollama_version"):
        e["ollama_version"], e["ollama_version_since"] = v, _ts()


def note_report(node, report, now=None):
    """`ollama_update`-Block aus dem Heartbeat des Agenten: Zwischenstand oder Fehler."""
    if not isinstance(report, dict) or not report.get("state") or state.REG is None or not node.fp:
        return
    e = state.REG.nodes.get(node.fp)
    if e is None:
        return
    now = now or time.time()
    u = e.get("ollama_update") or {}
    if u.get("state") == "done" and report["state"] != "failed":
        return
    if report.get("version") and u.get("version") and report["version"] != u["version"]:
        return   # alter Bericht zu einem frueheren Auftrag
    if u.get("state") == "failed" and report["state"] == "failed" and (report.get("message") or "") == (u.get("message") or ""):
        return   # derselbe Fehler, jeden Heartbeat wiederholt
    changed = report["state"] != u.get("state") or (report.get("message") or "") != (u.get("message") or "")
    if not changed:
        return
    u.update(state=report["state"], message=report.get("message") or "", t=now, ts=_ts(now), version=u.get("version") or report.get("version"))
    e["ollama_update"] = u
    state.REG.save()
    if report["state"] != "downloading" or not u.get("last_event") or u["last_event"] != "downloading":
        state.remember({"event": "ollama_update", "node": node.name, "state": report["state"], "version": u.get("version"), "reason": u.get("message")})
        u["last_event"] = report["state"]
    state.MQTT_DIRTY.append(True)
    (log.warning if report["state"] == "failed" else log.info)("node %s: Ollama-Update %s%s", node.name, report["state"], f" ({u['message']})" if u.get("message") else "")
    if report["state"] == "failed":
        node.draining_until = 0.0


def problems(now=None):
    """HA-Probleme: fehlgeschlagene oder haengende Ollama-Updates, gescheiterte Versionspruefung nur nach drei Tagen Stille."""
    now = now or time.time()
    out = []
    if state.REG is None:
        return out
    for e in state.REG.nodes.values():
        u = update_view(e, now)
        if u.get("state") == "failed":
            out.append(f"Ollama-Update auf {e['name']} fehlgeschlagen: {u.get('message') or '?'}")
        elif u.get("state") == "stalled":
            out.append(f"Ollama-Update auf {e['name']} haengt ({u.get('message')})")
    if LATEST.get("error") and _cfg().get("enabled") and (not LATEST.get("version") or now - _last_ok() > 3 * 86400):
        out.append(f"Ollama-Versionspruefung scheitert: {LATEST['error']}")
    return out


def _last_ok():
    """Zeitpunkt der letzten erfolgreichen Versionspruefung (checked wird auch bei Fehlern gesetzt)."""
    return LATEST.get("checked_ok") or 0.0


def pending_nodes(now=None):
    """{Knotenname: {current, available}} fuer alle Knoten, denen ein neueres Ollama fehlt (HA-Sensor, Metrik)."""
    out = {}
    if state.REG is None:
        return out
    for e in state.REG.nodes.values():
        if e.get("state") != "approved":
            continue
        u = update_view(e, now)
        if u.get("pending"):
            out[e["name"]] = {"current": u["current"], "available": u["available"]}
    return out


async def order_update(fp, reason="ui", force=False):
    """Auftrag an einen Knoten. Liefert (ok, Meldung)."""
    e = state.REG.nodes.get(fp)
    if e is None:
        return False, "unknown fingerprint"
    if e.get("state") != "approved":
        return False, "node is not approved"
    node = state.NODES.get(e["name"])
    if node is None or node.fp != fp or node.tunnel is None:
        return False, "agent not connected"
    version, f = available_for(e)
    if not f:
        facts = e.get("facts") or {}
        return False, f"no Ollama archive known for {facts.get('os')}/{facts.get('arch')}" + ("" if LATEST["version"] else " (version check has not succeeded yet)")
    cur = current_version(e)
    if cur == version and not force:
        return False, f"already running {version}"
    if cur and _vt(cur) > _vt(version) and not force:
        return False, f"node runs {cur}, newer than {version}"
    now = time.time()
    u = e.get("ollama_update") or {}
    if u.get("state") in RUNNING and now - (u.get("t") or 0) < STALL_S and not force:
        return False, f"order for {u.get('version')} running for {int((now - u['t']) // 60)} min ({u.get('state')})"
    if node.inflight > 0 and not force:
        return False, f"node is serving {node.inflight} request(s)"
    msg = {"t": "ollama-update", "version": version, "file": f["name"], "url": f["url"], "sha256": f["sha256"], "size": f.get("size", 0)}
    e["ollama_update"] = {"version": version, "state": "requested", "message": "", "t": now, "ts": _ts(now), "by": reason}
    node.draining_until = now + DRAIN_S
    state.REG.save()
    from .registry import send_ctl
    await send_ctl(node.tunnel, msg)
    state.remember({"event": "ollama_update", "node": e["name"], "state": "requested", "version": version, "reason": reason})
    state.MQTT_DIRTY.append(True)
    log.info("node %s: Ollama-Update %s -> %s angestossen (%s, %s)", e["name"], cur, version, reason, f["name"])
    return True, f"Ollama update to {version} started"


def in_window(cfg, now=None):
    """Liegt die Ortszeit im Fenster window_start..window_end (auch ueber Mitternacht)? Leeres Fenster = immer."""
    start, end = (cfg.get("window_start") or "").strip(), (cfg.get("window_end") or "").strip()
    if not start or not end:
        return True
    try:
        a = int(start[:2]) * 60 + int(start[3:5])
        b = int(end[:2]) * 60 + int(end[3:5])
    except ValueError:
        return True
    lt = time.localtime(now or time.time())
    cur = lt.tm_hour * 60 + lt.tm_min
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def canary_name(cfg):
    return cfg.get("canary") or (state.CFG.agent_update or {}).get("canary") or ""


def canary_clean(cfg, version, now):
    """Faehrt der Kanarienvogel diese Ollama-Version seit canary_clean_h Stunden und ist er online?"""
    name = canary_name(cfg)
    if not name or state.REG is None:
        return False
    e = next((x for x in state.REG.nodes.values() if x["name"] == name), None)
    node = state.NODES.get(name)
    if e is None or node is None or node.state == "offline":
        return False
    if e.get("ollama_version") != version or not e.get("ollama_version_since"):
        return False
    try:
        since = time.mktime(time.strptime(e["ollama_version_since"], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    return now - since >= float(cfg.get("canary_clean_h", 24)) * 3600


async def rollout_loop():
    """Alle 60 s: Versionspruefung faellig? Dann Knoten mit Policy ollama_auto_update im Nachtfenster nachziehen."""
    load_latest()
    await asyncio.sleep(45)
    while True:
        try:
            cfg = _cfg()
            if cfg.get("enabled") and time.time() - (LATEST.get("checked") or 0) >= float(cfg.get("check_interval_h", 6)) * 3600:
                await check_latest()
                if not LATEST.get("error"):
                    LATEST["checked_ok"] = LATEST["checked"]
            await rollout_once()
        except Exception as e:  # noqa: BLE001
            log.warning("ollama-update rollout: %s", e)
        await asyncio.sleep(60)


async def rollout_once(now=None):
    cfg = _cfg()
    if not cfg.get("enabled") or state.REG is None or not LATEST.get("version"):
        return
    now = now or time.time()
    if not in_window(cfg, now):
        return
    version = LATEST["version"]
    for fp, e in list(state.REG.nodes.items()):
        if e.get("state") != "approved" or not (e.get("policy") or {}).get("ollama_auto_update"):
            continue
        avail, f = available_for(e)
        cur = current_version(e)
        if not f or not cur or _vt(avail) <= _vt(cur):
            continue
        u = e.get("ollama_update") or {}
        if u.get("version") == version and u.get("state") in RUNNING and now - (u.get("t") or 0) < STALL_S:
            continue
        if u.get("version") == version and u.get("state") in ("failed", "stalled") and now - (u.get("t") or 0) < RETRY_S:
            continue
        is_canary = e["name"] == canary_name(cfg)
        if not is_canary and canary_name(cfg) and not canary_clean(cfg, version, now):
            continue
        node = state.NODES.get(e["name"])
        if node is None or node.tunnel is None or node.inflight > 0 or node.state != "free":
            continue
        ok, msg = await order_update(fp, reason="auto" + ("-canary" if is_canary else ""))
        log.info("ollama-update rollout %s: %s", e["name"], msg)
        if ok:
            return   # einer je Runde


def status_view(now=None):
    cfg = _cfg()
    return {"latest": {k: LATEST.get(k) for k in ("version", "checked_ts", "error", "source", "files")},
            "enabled": cfg.get("enabled"), "canary": canary_name(cfg), "canary_clean_h": cfg.get("canary_clean_h"),
            "window_start": cfg.get("window_start"), "window_end": cfg.get("window_end"), "in_window": in_window(cfg, now),
            "check_interval_h": cfg.get("check_interval_h"), "pending": pending_nodes(now)}


async def handle_status(request):
    """GET /admin/ollama-update: neueste Version, Fenster, ausstehende Knoten (UI)."""
    return web.json_response(status_view())


async def handle_check(request):
    """POST /admin/ollama-update/check: Versionspruefung jetzt (UI-Knopf, Tests)."""
    await check_latest()
    if not LATEST.get("error"):
        LATEST["checked_ok"] = LATEST["checked"]
    return web.json_response(status_view(), status=200 if not LATEST.get("error") else 502)
