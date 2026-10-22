"""Ollama update via the router (Router 0.3.0, Agent 0.12.0, 2026-09-26). Since Ollama runs on the node as a child of the Agent
service, the Tray-App updater no longer works (runs as the user, Pro-user installer). The router
takes over the updater role: it knows the latest version and when a node is free.

Flow
  1. Version check: every `check_interval_h` hours GET `release_url` (GitHub-API, latest release of ollama/ollama),
     plus the release's `sha256sum.txt` -> LATEST = {version, files: {"windows/amd64": {name, url, size, sha256}, ...}},
     persisted in <config-dir>/ollama-latest.json (survives restarts).
  2. Task (UI button or rollout loop): node admitted, connected, `free`, nothing inflight. Via the tunnel goes
     {"t": "ollama-update", version, file, url, sha256, size}. The Agent downloads the archive ITSELF from its fixed source
     (GitHub) and compares the hash with the sha256sum.txt there - the router can only choose the version, not the
     code (unlike the Agent update, there is no operator key, instead the fixed source in the Agent).
  3. Status comes in the Heartbeat (`ollama_update: {state, version, message}`): checking, downloading (with percent), extracting,
     swapping, applied, failed. If the Heartbeat (`ollama_version`) or the Poll (/api/version) reports the target version, the
     task is `done`. During the task, the node receives no new requests (Drain). Errors or silence -> HA Problem.
  4. Rollout: Policy `ollama_auto_update` per node, only in the night window (`window_start`..`window_end`, local time), canary
     first (`canary`, otherwise the one from the Agent update), the others only after `canary_clean_h` hours of clean operation.
"""
import asyncio
import os
import re
import time

import aiohttp
from aiohttp import web

from . import state
from .common import VERSION, load_json_file, log, save_json_file
from .outcome import bad_gateway, failed, invalid

ASSETS = {("windows", "amd64"): "ollama-windows-amd64.zip", ("windows", "arm64"): "ollama-windows-arm64.zip",
          ("linux", "amd64"): "ollama-linux-amd64.tar.zst", ("linux", "arm64"): "ollama-linux-arm64.tar.zst"}
LATEST = {"version": None, "checked": 0.0, "checked_ts": None, "files": {}, "error": None, "source": None}
RUNNING = ("requested", "checking", "downloading", "extracting", "ready", "swapping", "applied")
DRAIN_S = 1800     # Agents without approval step (< 0.15.0): no new requests to the node from task until swap
SWAP_DRAIN_S = 1800   # from "ready": how long it may take to drain, after which the router aborts (Agent waits 30 min)
SWAP_WAIT_S = 25 * 60
STALL_S = 3600     # without new status in the Heartbeat, the task is considered stalled
RETRY_S = 6 * 3600 # after failed/stalled, automatically retry only after 6 h
AGENT_FRESH_S = 600  # Ollama version reported by the Agent has 10 min priority over the Poll (/api/version every 5 min)


def _ts(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now or time.time()))


def _cfg():
    return state.CFG.ollama_update or {}


def _vt(v):
    """Version tuple for comparison: '0.34.4' -> (0, 34, 4); pre-release suffixes do not count."""
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "").split("-")[0].split("+")[0]))


def latest_path():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "ollama-latest.json")


def load_latest():
    d = load_json_file(latest_path(), "ollama-latest.json")
    if isinstance(d, dict) and d.get("version") and isinstance(d.get("files"), dict):
        LATEST.update({k: d[k] for k in ("version", "checked", "checked_ts", "files", "source") if k in d})


def save_latest():
    save_json_file(latest_path(), LATEST, "ollama-latest.json", indent=1)   # if it fails, LATEST remains in memory


def parse_checksums(txt):
    """Lines '<hex>  ./<file>' (also '*<file>' or without prefix) -> {file: hex}."""
    out = {}
    for line in txt.splitlines():
        f = line.split()
        if len(f) == 2 and len(f[0]) == 64:
            name = f[1].lstrip("*")
            out[name[2:] if name.startswith("./") else name] = f[0].lower()
    return out


async def check_latest():
    """Query latest version from the source (GitHub-API: tag_name, assets) including sha256sum.txt; result in LATEST."""
    url = _cfg().get("release_url") or ""
    found = await _fetch_release(url) if url else invalid("modes.ollama_update.release_url is empty")
    if failed(found):
        LATEST.update(error=found.message[:200], checked=time.time(), checked_ts=_ts())
        log.warning("ollama-update: version check: %s", LATEST["error"])
        return LATEST
    version, files = found
    changed = version != LATEST["version"]
    LATEST.update(version=version, files=files, error=None, source=url, checked=time.time(), checked_ts=_ts())
    save_latest()
    if changed:
        log.info("ollama-update: latest version %s (%s)", version, ", ".join(sorted(files)))
        state.MQTT_DIRTY.append(True)
    return LATEST


async def _fetch_release(url):
    """(version, {"os/arch": archive}) of the latest release or a Problem 502 with the reason."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"skirnir-router/{VERSION}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.get(url, headers=headers) as r:
                if r.status != 200:
                    return bad_gateway(f"HTTP {r.status} from {url}")
                rel = await r.json(content_type=None)
            if not isinstance(rel, dict):
                return bad_gateway(f"no release description from {url}")
            tag = str(rel.get("tag_name") or rel.get("name") or "")
            version = tag[1:] if tag.startswith("v") else tag
            if not re.match(r"^\d+(\.\d+){1,3}", version):
                return bad_gateway(f"tag_name {tag!r} is not a version")
            assets = {a["name"]: a for a in rel.get("assets") or [] if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")}
            sums = assets.get("sha256sum.txt")
            if not sums:
                return bad_gateway("release without sha256sum.txt")
            async with s.get(sums["browser_download_url"], headers={"User-Agent": headers["User-Agent"]}) as r:
                if r.status != 200:
                    return bad_gateway(f"sha256sum.txt: HTTP {r.status}")
                checks = parse_checksums(await r.text())
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:  # exception-ok: network boundary and the source's JSON
        return bad_gateway(f"{e.__class__.__name__}: {e}")
    files = {}
    for (osn, arch), name in ASSETS.items():
        a = assets.get(name)
        if a and name in checks:
            size = a.get("size") if isinstance(a.get("size"), int) else 0
            files[f"{osn}/{arch}"] = {"name": name, "url": a["browser_download_url"], "size": size, "sha256": checks[name]}
    return (version, files) if files else bad_gateway("no known archive in the release")


def available_for(e):
    """(latest version, archive) for the node's OS/Arch, or (None, None)."""
    facts = e.get("facts") or {}
    f = LATEST["files"].get(f"{facts.get('os')}/{facts.get('arch')}")
    return (LATEST["version"], f) if f else (None, None)


def current_version(e):
    """Last observed Ollama version of the node (Register; Heartbeat/Poll maintain it), otherwise from registration."""
    return e.get("ollama_version") or (e.get("facts") or {}).get("ollama_version") or None


def swap_gate(e):
    """Does the Agent swap only after approval (Fact `ollama_swap_gate`, Agent >= 0.15.0)? Then the node stays in routing during download
    and check, and the router releases the swap as soon as no request is running."""
    return (e.get("facts") or {}).get("ollama_swap_gate") is True


def update_running(node, now=None):
    """Is an Ollama update currently running on this node (task until restart)? Then no prewarming: the swap terminates Ollama."""
    if state.REG is None or not node.fp:
        return False
    u = (state.REG.nodes.get(node.fp) or {}).get("ollama_update") or {}
    return u.get("state") in RUNNING and (now or time.time()) - (u.get("t") or 0) < STALL_S


async def release_swap(node, version):
    """After "ready": remove node from routing, wait until no request is running, then release the swap.
    If the node does not drain within SWAP_WAIT_S, the router aborts (nothing swapped, node back in routing)."""
    node.draining_until = time.time() + SWAP_DRAIN_S
    try:
        deadline = time.time() + SWAP_WAIT_S
        while node.inflight > 0 and time.time() < deadline:
            await asyncio.sleep(0.5)
        e = state.REG.nodes.get(node.fp) if state.REG is not None else None
        u = (e or {}).get("ollama_update") or {}
        if u.get("version") != version or u.get("state") != "ready":
            return   # Task is ongoing, aborted, or replaced
        if node.inflight > 0:
            now = time.time()
            u.update(state="failed", message=f"Knoten wurde in {SWAP_WAIT_S // 60} min nicht leer ({node.inflight} Anfragen) - nicht getauscht",
                     t=now, ts=_ts(now))
            e["ollama_update"] = u
            state.REG.save()
            state.remember({"event": "ollama_update", "node": node.name, "state": "failed", "version": version, "reason": u["message"]})
            state.MQTT_DIRTY.append(True)
            node.draining_until = 0.0
            log.warning("node %s: Ollama swap to %s aborted - node did not become idle", node.name, version)
            return
        if node.tunnel is None:
            log.warning("node %s: Ollama swap to %s not released - no tunnel", node.name, version)
            return
        from .registry import send_ctl
        await send_ctl(node.tunnel, {"t": "ollama-swap", "version": version})
        log.info("node %s: Ollama swap to %s released (node idle)", node.name, version)
    finally:
        node.swap_pending = None


def managed(e):
    """Does the Agent of this node update Ollama? The Agent reports this itself on every registration (Fact `ollama_managed`, from
    Agent 0.12.1: Ollama is one of its children). Without the Fact, the router sends no Ollama tasks."""
    return (e.get("facts") or {}).get("ollama_managed") is True


def unmanaged_reason(e):
    """None = managed; 'tray' = Ollama does not run as a child of the Agent (typically the Tray-App under the user, which updates itself);
    'agent' = the Agent does not report it (older than 0.12.1)."""
    f = (e.get("facts") or {}).get("ollama_managed")
    return None if f is True else "tray" if f is False else "agent"


def update_view(e, now=None):
    """For /admin/nodes, UI and HA: running version, latest version, task status, whether the Agent manages Ollama."""
    now = now or time.time()
    cur = current_version(e)
    avail, _ = available_for(e)
    u = dict(e.get("ollama_update") or {})
    if u.get("state") in RUNNING and now - (u.get("t") or 0) > STALL_S:
        u["state"], u["message"] = "stalled", f"no report for {int((now - u['t']) // 60)} min"
    m = managed(e)
    if not m and u.get("state") not in RUNNING:
        u = {}   # Task status only where the router sends Ollama updates (an old rejection is not an HA Problem)
    return {"current": cur, "available": avail, "pending": bool(avail and cur and _vt(avail) > _vt(cur)), "managed": m,
            "unmanaged_reason": unmanaged_reason(e),
            "auto": bool((e.get("policy") or {}).get("ollama_auto_update")), "version_since": e.get("ollama_version_since"), **u}


def note_version(node, version, now=None, source="poll"):
    """Observed Ollama version of a node (Poll /api/version or Agent Heartbeat): record change, close open
    task. The Agent report has 10 min priority over the Poll (the Poll sees the new version later)."""
    if not version or version == "?" or state.REG is None or not node.fp:
        return
    now = now or time.time()
    if source == "agent":
        node.ollama_version_agent_ts = now
    elif now - getattr(node, "ollama_version_agent_ts", 0.0) < AGENT_FRESH_S:
        return   # Agent just reported: the Poll is the older view
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
        log.info("node %s: Ollama update to %s completed", node.name, version)
        if state.CFG.prewarm_on_online and node.state == "free":   # the prewarming at restart was skipped, the update was still running
            from . import poll
            state.spawn(poll.prewarm(node, 0, "after Ollama update"))


def note_hello(e, facts):
    """Agent registration: adopt Ollama version from Facts if the Register does not know it yet."""
    e.pop("ollama_managed", None)   # Altlast von Router 0.3.2 (gelernt statt gemeldet)
    v = (facts or {}).get("ollama_version")
    if v and not e.get("ollama_version"):
        e["ollama_version"], e["ollama_version_since"] = v, _ts()


def note_report(node, report, now=None):
    """`ollama_update` block from the Agent Heartbeat: intermediate status or error."""
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
        return   # old report for a previous task
    if report["state"] == "ready" and swap_gate(e) and getattr(node, "swap_pending", None) != u.get("version"):
        # every "ready" report without an ongoing approval starts one (also after a router restart while waiting)
        node.swap_pending = u.get("version") or report.get("version")
        state.spawn(release_swap(node, node.swap_pending))
    if u.get("state") == "failed" and report["state"] == "failed" and (report.get("message") or "") == (u.get("message") or ""):
        return   # the same error, repeated every Heartbeat
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
    """HA Problems: failed or stalled Ollama updates, failed version check only after three days of silence."""
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
    """Time of the last successful version check (checked is also set on errors)."""
    return LATEST.get("checked_ok") or 0.0


def pending_nodes(now=None):
    """{node_name: {current, available}} for all nodes missing a newer Ollama (HA sensor, metric)."""
    out = {}
    if state.REG is None:
        return out
    for e in state.REG.nodes.values():
        if e.get("state") != "approved":
            continue
        u = update_view(e, now)
        if u.get("pending") and u.get("managed"):
            out[e["name"]] = {"current": u["current"], "available": u["available"]}
    return out


async def order_update(fp, reason="ui", force=False):
    """Task for a node. Returns (ok, message)."""
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
    av = (e.get("facts") or {}).get("agent_version")
    if not agent_capable(av):
        return False, f"agent {av} cannot update Ollama (needs agent 0.12.0 or newer)"
    why = unmanaged_reason(e)
    if why == "tray":   # not even with force: the agent might not be able to
        return False, "Ollama is not a child of this agent (tray app); it updates itself"
    if why == "agent" and not force:
        return False, f"agent {av} does not report whether it manages Ollama (needs agent 0.12.1 or newer)"
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
    if not swap_gate(e):   # old agent swaps immediately after download: clear node from the start
        node.draining_until = now + DRAIN_S
    state.REG.save()
    from .registry import send_ctl
    await send_ctl(node.tunnel, msg)
    state.remember({"event": "ollama_update", "node": e["name"], "state": "requested", "version": version, "reason": reason})
    state.MQTT_DIRTY.append(True)
    log.info("node %s: Ollama update %s -> %s ordered (%s, %s)", e["name"], cur, version, reason, f["name"])
    return True, f"Ollama update to {version} started"


def agent_capable(agent_version):
    """Does the agent understand the task? Since 0.12.0; a version without numbers (test agent) is considered capable."""
    t = _vt(agent_version)
    return not t or t >= (0, 12)


def _local(cfg, now):
    """Time for the window: in `timezone` (e.g. Europe/Zurich), otherwise router local time. Containers often run on UTC -
    2026-09-26 the first Ollama update ran at 06:58 Swiss time instead of the 02:00-05:00 window."""
    tz = (cfg.get("timezone") or "").strip()
    if tz:
        import zoneinfo
        from datetime import datetime
        if tz in zoneinfo.available_timezones():   # check beforehand instead of catching exceptions
            return datetime.fromtimestamp(now, zoneinfo.ZoneInfo(tz)).timetuple()
        if not _TZ_WARNED:
            log.warning("ollama-update: time zone %r unknown here (no time zone database?) - window in the router's local time", tz)
            _TZ_WARNED.append(tz)
    return time.localtime(now)


_TZ_WARNED = []
HHMM = re.compile(r"\d{2}:\d{2}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")   # _ts(), written by the router itself


def in_window(cfg, now=None):
    """Is the time (timezone `timezone`, otherwise router local time) within window_start..window_end (also across
    midnight)? Empty window = always."""
    start, end = (cfg.get("window_start") or "").strip(), (cfg.get("window_end") or "").strip()
    if not (HHMM.fullmatch(start) and HHMM.fullmatch(end)):   # empty or unreadable = no window
        return True
    a = int(start[:2]) * 60 + int(start[3:5])
    b = int(end[:2]) * 60 + int(end[3:5])
    lt = _local(cfg, now or time.time())
    cur = lt.tm_hour * 60 + lt.tm_min
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def canary_name(cfg):
    return cfg.get("canary") or (state.CFG.agent_update or {}).get("canary") or ""


def canary_clean(cfg, version, now):
    """Has the canary been running this Ollama version for canary_clean_h hours and is it online?"""
    name = canary_name(cfg)
    if not name or state.REG is None:
        return False
    e = next((x for x in state.REG.nodes.values() if x["name"] == name), None)
    node = state.NODES.get(name)
    if e is None or node is None or node.state == "offline":
        return False
    if e.get("ollama_version") != version or not STAMP.fullmatch(str(e.get("ollama_version_since") or "")):
        return False
    since = time.mktime(time.strptime(e["ollama_version_since"], "%Y-%m-%dT%H:%M:%S"))
    return now - since >= float(cfg.get("canary_clean_h", 24)) * 3600


async def rollout_loop():
    """Every 60 s: version check due? Then update nodes with policy ollama_auto_update in the night window."""
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
        except Exception as e:  # noqa: BLE001  # exception-ok: background loop; one failed round must not end the rollout, next try in 60 s
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
        if not f or not cur or _vt(avail) <= _vt(cur) or not managed(e):
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
            "window_start": cfg.get("window_start"), "window_end": cfg.get("window_end"), "timezone": cfg.get("timezone") or "",
            "in_window": in_window(cfg, now),
            "check_interval_h": cfg.get("check_interval_h"), "pending": pending_nodes(now)}


async def handle_status(request):
    """GET /admin/ollama-update: latest version, window, pending nodes (UI)."""
    return web.json_response(status_view())


async def handle_check(request):
    """POST /admin/ollama-update/check: version check now (UI button, tests)."""
    await check_latest()
    if not LATEST.get("error"):
        LATEST["checked_ok"] = LATEST["checked"]
    return web.json_response(status_view(), status=200 if not LATEST.get("error") else 502)
