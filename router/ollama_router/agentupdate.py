"""Agent-Update ueber den Router (2026-09-25): der Router verteilt signierte Agent-Binaries, der Agent holt sie durch die
bestehende Verbindung und tauscht sich selbst. Grund: Knoten ohne Fernzugriff (nur ausgehender Tunnel) und wachsende Zahl
von Geraeten.

Ablauf
  1. `deploy.py --agent` legt die Binaries samt Manifest nach <config-dir>/agent/ (manifest.json = {"manifest": {...},
     "signature": base64}); die Signatur stammt vom Betreiber-Schluessel im Ops-Ordner, NICHT vom Router - ein kompromittierter
     Router kann Inferenz umlenken, aber keinen Code auf die Knoten bringen.
  2. Auftrag (UI-Knopf oder Rollout-Schleife): Knoten muss freigegeben, verbunden und im Leerlauf sein. Der Router schickt
     durch den Tunnel {"t": "update", version, file, url, sha256, size, token, manifest, signature}; das Token gilt 15 min
     fuer den Download von /v1/agent/binary/<file> (kein Basic Auth, der Agent hat keins).
  3. Der Agent prueft Signatur und SHA-256, testet die neue Binary (`version`), tauscht und startet neu; sein Heartbeat
     traegt `update: {state, version, message}`. Meldet er sich mit der neuen Version, gilt der Auftrag als erledigt.
  4. Rollout: Policy `auto_update` je Knoten; `modes.agent_update.canary` bekommt neue Versionen zuerst, alle anderen erst,
     wenn der Kanarienvogel die Version `canary_clean_h` Stunden faehrt. Waehrend eines Auftrags bekommt der Knoten keine
     neuen Anfragen (Drain, 10 min). Haengt ein Auftrag 15 min oder meldet der Agent einen Fehler -> HA-Problem.
"""
import asyncio
import base64
import json
import os
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


def agent_dir():
    return os.path.join(os.path.dirname(os.path.abspath(state.CFG.path)), "agent")


def canonical(manifest):
    """Signierte Form: kompaktes JSON mit sortierten Schluesseln (deploy.py signiert genau diese Bytes)."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_manifest():
    """Manifest aus <agent_dir>/manifest.json (Cache nach mtime). None = kein Update hinterlegt oder unlesbar."""
    p = os.path.join(agent_dir(), "manifest.json")
    try:
        mtime = os.stat(p).st_mtime
    except FileNotFoundError:
        _MANIFEST.update(mtime=None, data=None)
        return None
    if _MANIFEST["mtime"] == mtime:
        return _MANIFEST["data"]
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
        m = raw["manifest"]
        if not m.get("version") or not isinstance(m.get("files"), list):
            raise ValueError("version/files fehlen")
        data = {"manifest": m, "signature": raw.get("signature", ""), "raw": raw}
    except Exception as e:  # noqa: BLE001
        log.warning("agent/manifest.json unlesbar: %s", e)
        data = None
    _MANIFEST.update(mtime=mtime, data=data)
    return data


def file_for(manifest, os_name, arch):
    for f in manifest.get("files", []):
        if f.get("os") == os_name and f.get("arch") == arch:
            return f
    return None


def verify_with(public_key_b64, manifest, signature_b64):
    """Optionale Selbstpruefung auf dem Router (wenn modes.agent_update.public_key gesetzt ist): dieselbe Pruefung wie im Agenten."""
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(signature_b64), canonical(manifest))


def available_for(e):
    """(Manifest-Version, Datei) fuer den Knoten, oder (None, None), wenn nichts Passendes hinterlegt ist."""
    m = load_manifest()
    if not m:
        return None, None
    facts = e.get("facts") or {}
    f = file_for(m["manifest"], facts.get("os"), facts.get("arch"))
    return (m["manifest"]["version"], f) if f else (None, None)


def update_view(e, now=None):
    """Fuer /admin/nodes und HA: aktuelle Version, verfuegbare Version, Auftragsstand."""
    now = now or time.time()
    facts = e.get("facts") or {}
    avail, _ = available_for(e)
    u = dict(e.get("update") or {})
    if u.get("state") in ("requested", "downloading", "applied") and now - (u.get("t") or 0) > STALL_S:
        u["state"], u["message"] = "stalled", f"keine Rueckmeldung seit {int((now - u['t']) // 60)} min"
    return {"current": facts.get("agent_version"), "available": avail, "pending": bool(avail and avail != facts.get("agent_version")),
            "auto": bool((e.get("policy") or {}).get("auto_update")), "version_since": e.get("version_since"), **u}


def note_hello(e, facts, now=None):
    """Beim Anmelden eines Agenten: Versionswechsel festhalten und einen offenen Auftrag abschliessen."""
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
        log.info("node %s: Agent-Update auf %s abgeschlossen", e["name"], v)
    node = state.NODES.get(e["name"])
    if node is not None:
        node.draining_until = 0.0


def note_report(node, report, now=None):
    """`update`-Block aus dem Heartbeat des Agenten: Zwischenstand (downloading, applied) oder Fehler."""
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
        return   # alter Bericht zu einem frueheren Auftrag
    changed = report["state"] != u.get("state") or (report.get("message") or "") != (u.get("message") or "")
    if changed:
        u.update(state=report["state"], message=report.get("message") or "", t=now, ts=time.strftime("%Y-%m-%dT%H:%M:%S"), version=u.get("version") or report.get("version"))
        e["update"] = u
        state.REG.save()
        state.remember({"event": "agent_update", "node": node.name, "state": report["state"], "version": u.get("version"), "reason": u.get("message")})
        state.MQTT_DIRTY.append(True)
        (log.warning if report["state"] == "failed" else log.info)("node %s: Agent-Update %s%s", node.name, report["state"], f" ({u['message']})" if u.get("message") else "")
        if report["state"] == "failed":
            node.draining_until = 0.0


def problems(now=None):
    """HA-Probleme: fehlgeschlagene oder haengende Updates."""
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
    """Auftrag an einen Knoten. Liefert (ok, Meldung)."""
    e = state.REG.nodes.get(fp)
    if e is None:
        return False, "unbekannter Fingerprint"
    if e.get("state") != "approved":
        return False, "Knoten ist nicht freigegeben"
    node = state.NODES.get(e["name"])
    if node is None or node.fp != fp or node.tunnel is None:
        return False, "Agent nicht verbunden"
    m = load_manifest()
    if not m:
        return False, "kein Agent-Manifest hinterlegt (deploy.py --agent)"
    facts = e.get("facts") or {}
    f = file_for(m["manifest"], facts.get("os"), facts.get("arch"))
    if not f:
        return False, f"keine Binary fuer {facts.get('os')}/{facts.get('arch')} im Manifest"
    version = m["manifest"]["version"]
    if version == facts.get("agent_version") and not force:
        return False, f"laeuft schon mit {version}"
    u = e.get("update") or {}
    if u.get("state") in ("requested", "downloading", "applied") and time.time() - (u.get("t") or 0) < STALL_S and not force:
        return False, f"Auftrag auf {u.get('version')} laeuft seit {int((time.time() - u['t']) // 60)} min"
    if node.inflight > 0 and not force:
        return False, f"Knoten bearbeitet gerade {node.inflight} Anfrage(n)"
    pub = (state.CFG.agent_update or {}).get("public_key")
    if pub:
        try:
            verify_with(pub, m["manifest"], m["signature"])
        except (InvalidSignature, ValueError) as ex:
            return False, f"Manifest-Signatur ungueltig ({ex.__class__.__name__}) - nichts geschickt"
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
    log.info("node %s: Agent-Update auf %s angestossen (%s, %s)", e["name"], version, reason, f["name"])
    return True, f"Update auf {version} angestossen"


async def handle_binary(request):
    """GET /v1/agent/binary/{name} mit Bearer-Token aus dem Auftrag (ohne Basic Auth; der Agent hat keine UI-Zugangsdaten)."""
    name = request.match_info["name"]
    auth = request.headers.get("Authorization", "")
    tok = auth[7:] if auth.startswith("Bearer ") else ""
    t = TOKENS.get(tok)
    if not t or t["expires"] < time.time() or t["file"] != name or t["uses"] <= 0:
        return web.json_response({"error": "token ungueltig oder abgelaufen"}, status=403)
    if "/" in name or "\\" in name or name.startswith("."):
        return web.json_response({"error": "ungueltiger Dateiname"}, status=400)
    path = os.path.join(agent_dir(), name)
    if not os.path.isfile(path):
        return web.json_response({"error": "Datei fehlt auf dem Router"}, status=404)
    t["uses"] -= 1
    e = state.REG.nodes.get(t["fp"]) if state.REG else None
    if e is not None and (e.get("update") or {}).get("state") == "requested":
        e["update"].update(state="downloading", t=time.time(), ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
    log.info("agent-update: %s laedt %s", e["name"] if e else t["fp"][:12], name)
    return web.FileResponse(path, headers={"Content-Type": "application/octet-stream"})


async def handle_manifest(request):
    """GET /admin/agent-update: Manifest und Rollout-Sicht fuer die UI."""
    m = load_manifest()
    cfg = state.CFG.agent_update or {}
    return web.json_response({"manifest": m["manifest"] if m else None, "signed": bool(m and m.get("signature")),
                             "canary": cfg.get("canary"), "canary_clean_h": cfg.get("canary_clean_h"), "enabled": cfg.get("enabled"),
                             "dir": agent_dir()})


def canary_clean(cfg, version, now):
    """Faehrt der Kanarienvogel diese Version seit canary_clean_h Stunden und ist er online?"""
    name = cfg.get("canary")
    if not name or state.REG is None:
        return False
    e = next((x for x in state.REG.nodes.values() if x["name"] == name), None)
    node = state.NODES.get(name)
    if e is None or node is None or node.state == "offline":
        return False
    if (e.get("facts") or {}).get("agent_version") != version or not e.get("version_since"):
        return False
    try:
        since = time.mktime(time.strptime(e["version_since"], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False
    return now - since >= float(cfg.get("canary_clean_h", 24)) * 3600


async def rollout_loop():
    """Alle 60 s: Knoten mit Policy auto_update auf die Manifest-Version bringen - erst der Kanarienvogel, die anderen
    einer nach dem anderen, sobald der Kanarienvogel die Version lange genug sauber faehrt."""
    await asyncio.sleep(30)
    while True:
        try:
            await rollout_once()
        except Exception as e:  # noqa: BLE001
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
        if u.get("version") == version and u.get("state") in ("failed", "stalled") and now - (u.get("t") or 0) < 6 * 3600:
            continue   # nach einem Fehlschlag frueh. nach 6 h wieder probieren (oder von Hand)
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
