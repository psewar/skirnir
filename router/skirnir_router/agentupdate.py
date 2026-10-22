"""Agent update via router (2026-09-25): the router distributes signed agent binaries; the agent fetches them over the
existing connection and replaces itself. Reason: nodes without remote access (outbound tunnel only) and growing device count.

Flow
  1. `deploy.py --agent` places binaries and manifest in <config-dir>/agent/ (manifest.json = {"manifest": {...},
     "signature": base64}); signature comes from the operator key in the ops folder, NOT the router - a compromised
     router can redirect inference but cannot push code to nodes.
  2. Request (UI button or rollout loop): node must be approved, connected, and idle. Router sends via tunnel {"t": "update", version, file, url, sha256, size, token, manifest, signature}; token valid 15 min
     for download from /v1/agent/binary/<file> (no Basic Auth, agent has none).
  3. Agent verifies signature and SHA-256, tests new binary (`version`), swaps and restarts; its Heartbeat
     carries `update: {state, version, message}`. Once it reports the new version, the request is complete.
  4. Rollout: Policy `auto_update` per node; `modes.agent_update.canary` gets new versions first, others only
     after the canary has run the version for `canary_clean_h` hours. During a request, the node receives no
     new requests (Drain, 10 min). If a request hangs 15 min or the agent reports an error -> HA Problem.
"""
import asyncio
import base64
import json
import os
import re
import secrets
import time

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import state
from .common import log

TOKENS = {}          # token -> {"fp", "file", "expires", "uses"}
TOKEN_TTL_S = 900
DRAIN_S = 600
STALL_S = 900
_MANIFEST = {"mtime": None, "data": None}
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")   # version_since, written by the router itself


def agent_dir():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "agent")


def canonical(manifest):
    """Signed form: compact JSON with sorted keys (deploy.py signs exactly these bytes)."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_manifest():
    """Manifest from <agent_dir>/manifest.json (cache by mtime). None = no update stored or unreadable."""
    p = os.path.join(agent_dir(), "manifest.json")
    try:
        mtime = os.stat(p).st_mtime
    except FileNotFoundError:  # exception-ok: stat is the existence check; a separate check would race with deploy.py
        _MANIFEST.update(mtime=None, data=None)
        return None
    if _MANIFEST["mtime"] == mtime:
        return _MANIFEST["data"]
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:  # exception-ok: file system and the file's JSON (JSONDecodeError, UnicodeDecodeError)
        log.warning("agent/manifest.json unreadable: %s", e)
        raw = None
    m = raw.get("manifest") if isinstance(raw, dict) else None
    data = None
    if isinstance(m, dict) and m.get("version") and isinstance(m.get("files"), list):
        data = {"manifest": m, "signature": raw.get("signature", ""), "raw": raw}
    elif raw is not None:
        log.warning("agent/manifest.json unreadable: manifest with version/files missing")
    _MANIFEST.update(mtime=mtime, data=data)
    return data


def file_for(manifest, os_name, arch):
    for f in manifest.get("files", []):
        if f.get("os") == os_name and f.get("arch") == arch:
            return f
    return None


def verify_with(public_key_b64, manifest, signature_b64):
    """Optional self-check on the router (if modes.agent_update.public_key is set): same check as in the agent.
    None if signature matches, otherwise the reason."""
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), canonical(manifest))
    except InvalidSignature:  # exception-ok: cryptography reports a wrong signature only as an exception
        return "signature does not match"
    except ValueError:  # exception-ok: base64 and cryptography report a malformed key or signature only as an exception
        return "key or signature malformed"
    return None


def available_for(e):
    """(Manifest version, file) for the node, or (None, None) if nothing suitable is stored."""
    m = load_manifest()
    if not m:
        return None, None
    facts = e.get("facts") or {}
    f = file_for(m["manifest"], facts.get("os"), facts.get("arch"))
    return (m["manifest"]["version"], f) if f else (None, None)


def update_view(e, now=None):
    """For /admin/nodes and HA: current version, available version, request status."""
    now = now or time.time()
    facts = e.get("facts") or {}
    avail, _ = available_for(e)
    u = dict(e.get("update") or {})
    if u.get("state") in ("requested", "downloading", "applied") and now - (u.get("t") or 0) > STALL_S:
        u["state"], u["message"] = "stalled", f"no report for {int((now - u['t']) // 60)} min"
    return {"current": facts.get("agent_version"), "available": avail, "pending": bool(avail and avail != facts.get("agent_version")),
            "auto": bool((e.get("policy") or {}).get("auto_update")), "version_since": e.get("version_since"), **u}


def note_hello(e, facts, now=None):
    """On agent login: record version change and close any open request."""
    now = now or time.time()
    v = facts.get("agent_version")
    if v and v != (e.get("facts") or {}).get("agent_version"):
        e["version_since"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    u = e.get("update") or {}
    if u.get("version") and u.get("state") not in ("done", "failed") and v == u["version"]:
        u.update(state="done", message=f"laeuft mit {v}", t=now, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
        e["update"] = u
        state.remember({"event": "agent_update", "node": e["name"], "state": "done", "version": v})
        state.MQTT_DIRTY.append(True)
        log.info("node %s: agent update to %s completed", e["name"], v)
    node = state.NODES.get(e["name"])
    if node is not None:
        node.draining_until = 0.0


def note_report(node, report, now=None):
    """`update` block from the agent's Heartbeat: progress (downloading, applied) or error."""
    if not isinstance(report, dict) or not report.get("state") or state.REG is None or not node.fp:
        return
    e = state.REG.nodes.get(node.fp)
    if e is None:
        return
    now = now or time.time()
    u = e.get("update") or {}
    if u.get("state") in ("done",) and report["state"] != "failed":
        return
    if report.get("version") and u.get("version") and report["version"] != u["version"]:
        return   # old report for a previous task
    kind = report.get("kind") if isinstance(report.get("kind"), str) else None   # fixed error type, Agent >= 0.19.1 ("" = none)
    changed = report["state"] != u.get("state") or (report.get("message") or "") != (u.get("message") or "") or kind != u.get("kind")
    if changed:
        u.update(state=report["state"], message=report.get("message") or "", t=now, ts=time.strftime("%Y-%m-%dT%H:%M:%S"), version=u.get("version") or report.get("version"))
        if kind is None:
            u.pop("kind", None)   # older agent: no field, retry_after falls back to the message text
        else:
            u["kind"] = kind
        e["update"] = u
        state.REG.save()
        state.remember({"event": "agent_update", "node": node.name, "state": report["state"], "version": u.get("version"), "reason": u.get("message")})
        state.MQTT_DIRTY.append(True)
        (log.warning if report["state"] == "failed" else log.info)("node %s: Agent-Update %s%s", node.name, report["state"], f" ({u['message']})" if u.get("message") else "")
        if report["state"] == "failed":
            node.draining_until = 0.0


def problems(now=None):
    """HA Problems: failed or hanging updates."""
    now = now or time.time()
    out = []
    if state.REG is None:
        return out
    for e in state.REG.nodes.values():
        u = update_view(e, now)
        if u.get("state") == "failed":
            out.append(f"Agent-Update auf {e['name']} fehlgeschlagen: {u.get('message') or '?'}")
        elif u.get("state") == "stalled":
            out.append(f"Agent-Update auf {e['name']} haengt ({u.get('message')})")
    return out


def make_token(fp, name):
    tok = secrets.token_urlsafe(32)
    now = time.time()
    for t in [t for t, v in TOKENS.items() if v["expires"] < now]:
        del TOKENS[t]
    TOKENS[tok] = {"fp": fp, "file": name, "expires": now + TOKEN_TTL_S, "uses": 3}
    return tok


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
    m = load_manifest()
    if not m:
        return False, "no agent manifest on the router (deploy.py --agent)"
    facts = e.get("facts") or {}
    f = file_for(m["manifest"], facts.get("os"), facts.get("arch"))
    if not f:
        return False, f"no binary for {facts.get('os')}/{facts.get('arch')} in the manifest"
    version = m["manifest"]["version"]
    if version == facts.get("agent_version") and not force:
        return False, f"already running {version}"
    u = e.get("update") or {}
    if u.get("state") in ("requested", "downloading", "applied") and time.time() - (u.get("t") or 0) < STALL_S and not force:
        return False, f"order for {u.get('version')} running for {int((time.time() - u['t']) // 60)} min"
    if node.inflight > 0 and not force:
        return False, f"node is serving {node.inflight} request(s)"
    pub = (state.CFG.agent_update or {}).get("public_key")
    why = verify_with(pub, m["manifest"], m["signature"]) if pub else None
    if why:
        return False, f"manifest signature invalid ({why}), nothing sent"
    token = make_token(fp, f["name"])
    url = f"{state.CFG.public_url.rstrip('/')}/v1/agent/binary/{f['name']}"
    msg = {"t": "update", "version": version, "file": f["name"], "url": url, "sha256": f["sha256"], "size": f.get("size", 0),
           "token": token, "manifest": canonical(m["manifest"]).decode("utf-8"), "signature": m["signature"]}
    now = time.time()
    e["update"] = {"version": version, "state": "requested", "message": "", "t": now, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": reason}
    node.draining_until = now + DRAIN_S
    state.REG.save()
    from .registry import send_ctl
    await send_ctl(node.tunnel, msg)
    state.remember({"event": "agent_update", "node": e["name"], "state": "requested", "version": version, "reason": reason})
    state.MQTT_DIRTY.append(True)
    log.info("node %s: agent update to %s ordered (%s, %s)", e["name"], version, reason, f["name"])
    return True, f"update to {version} started"


async def handle_binary(request):
    """GET /v1/agent/binary/{name} with Bearer token from the request (no Basic Auth; agent has no UI credentials)."""
    name = request.match_info["name"]
    auth = request.headers.get("Authorization", "")
    tok = auth[7:] if auth.startswith("Bearer ") else ""
    t = TOKENS.get(tok)
    if not t or t["expires"] < time.time() or t["file"] != name or t["uses"] <= 0:
        return web.json_response({"error": "token invalid or expired"}, status=403)
    if "/" in name or "\\" in name or name.startswith("."):
        return web.json_response({"error": "invalid file name"}, status=400)
    path = os.path.join(agent_dir(), name)
    if not os.path.isfile(path):
        return web.json_response({"error": "file missing on the router"}, status=404)
    t["uses"] -= 1
    e = state.REG.nodes.get(t["fp"]) if state.REG else None
    if e is not None and (e.get("update") or {}).get("state") == "requested":
        e["update"].update(state="downloading", t=time.time(), ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
    log.info("agent-update: %s downloads %s", e["name"] if e else t["fp"][:12], name)
    return web.FileResponse(path, headers={"Content-Type": "application/octet-stream"})


async def handle_manifest(request):
    """GET /admin/agent-update: Manifest and rollout view for the UI."""
    m = load_manifest()
    cfg = state.CFG.agent_update or {}
    return web.json_response({"manifest": m["manifest"] if m else None, "signed": bool(m and m.get("signature")),
                             "canary": cfg.get("canary"), "canary_clean_h": cfg.get("canary_clean_h"), "enabled": cfg.get("enabled"),
                             "dir": agent_dir()})


def canary_clean(cfg, version, now):
    """Has the canary been running this version for canary_clean_h hours and is it online?"""
    name = cfg.get("canary")
    if not name or state.REG is None:
        return False
    e = next((x for x in state.REG.nodes.values() if x["name"] == name), None)
    node = state.NODES.get(name)
    if e is None or node is None or node.state == "offline":
        return False
    if (e.get("facts") or {}).get("agent_version") != version or not STAMP.fullmatch(str(e.get("version_since") or "")):
        return False
    since = time.mktime(time.strptime(e["version_since"], "%Y-%m-%dT%H:%M:%S"))
    return now - since >= float(cfg.get("canary_clean_h", 24)) * 3600


APP_CONTROL_RETRY_S = 1800


def retry_after(u):
    """Wait time after failure: 30 min if Smart App Control / WDAC blocked the new binary (the cloud verdict on an
    unsigned file often flips within minutes to hours: 2026-09-26 0.11.0 after 11 min, 0.12.0 after < 6 h), otherwise 6 h.
    Agent >= 0.19.1 reports the type as field `kind` (detected via Windows error code, agent/updater.go updateKind)."""
    if "kind" in u:
        return APP_CONTROL_RETRY_S if u["kind"] == "app_control" else 6 * 3600
    # Fallback for agents before 0.19.1 that do not send `kind`: only their (localized) message text reveals the block.
    # Remove once all nodes run Agent >= 0.19.1.
    msg = (u.get("message") or "").lower()
    if "application control" in msg or "anwendungssteuerung" in msg:
        return APP_CONTROL_RETRY_S
    return 6 * 3600


async def rollout_loop():
    """Every 60 s: bring nodes with policy auto_update to the manifest version - canary first, others
    one by one, once the canary has run the version cleanly long enough."""
    await asyncio.sleep(30)
    while True:
        try:
            await rollout_once()
        except Exception as e:  # noqa: BLE001  # exception-ok: background loop; one failed round must not end the rollout, next try in 60 s
            log.warning("agent-update rollout: %s", e)
        await asyncio.sleep(60)


async def rollout_once(now=None):
    cfg = state.CFG.agent_update or {}
    if not cfg.get("enabled", True) or state.REG is None:
        return
    m = load_manifest()
    if not m:
        return
    now = now or time.time()
    version = m["manifest"]["version"]
    for fp, e in list(state.REG.nodes.items()):
        if e.get("state") != "approved" or not (e.get("policy") or {}).get("auto_update"):
            continue
        facts = e.get("facts") or {}
        if facts.get("agent_version") == version or not file_for(m["manifest"], facts.get("os"), facts.get("arch")):
            continue
        u = e.get("update") or {}
        if u.get("version") == version and u.get("state") in ("requested", "downloading", "applied") and now - (u.get("t") or 0) < STALL_S:
            continue
        if u.get("version") == version and u.get("state") in ("failed", "stalled") and now - (u.get("t") or 0) < retry_after(u):
            continue   # early after a failure. retry after 6 h (or manually)
        is_canary = e["name"] == cfg.get("canary")
        if not is_canary and cfg.get("canary") and not canary_clean(cfg, version, now):
            continue
        node = state.NODES.get(e["name"])
        if node is None or node.tunnel is None or node.inflight > 0 or node.state != "free":
            continue
        ok, msg = await order_update(fp, reason="auto" + ("-canary" if is_canary else ""))
        log.info("agent-update rollout %s: %s", e["name"], msg)
        if ok:
            return   # einer je Runde
