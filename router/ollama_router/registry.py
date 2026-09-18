"""Knotenregister mit Schluessel-Identitaet: Anmeldung, Freigabe, Sperren, Provisionierung."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time

from aiohttp import web

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # Debian: python3-cryptography
except ImportError:  # pragma: no cover
    Ed25519PublicKey = None

from . import nodes, state, tunnel
from .common import VERSION, log, safe_node_name


class NodeRegistry:
    def __init__(self, path):
        self.path = path
        self.nodes = {}
        self.dirty = False
        self.last_save = 0.0

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.nodes = json.load(f).get("nodes", {})
        except FileNotFoundError:
            self.nodes = {}
        except Exception as e:  # noqa: BLE001
            log.error("nodes.json unlesbar (%s) - starte mit leerem Register", e)
            self.nodes = {}
        self.drop_stale_learned()
        return self

    def drop_stale_learned(self):
        """Einmalig beim Start: Gelerntes im alten Format wegwerfen (Eimer = eine Zahl statt {n,sum,min}).

        Es entstand ohne den Ruhe-Filter und ist systematisch verdorben: gelernt wird nur, wenn Ollama nichts
        haelt - auf einem Knoten mit dauerhaft warmem Modell ist das fast nur waehrend eines Spiels, weil erst
        `unload_on_busy` das Modell aus dem VRAM nimmt. Der Lerner sah die Welt also vorwiegend im Spielzustand.
        Wegwerfen statt umrechnen; bis wieder genug ruhige Proben da sind, traegt der Policy-Wert."""
        for fp, e in self.nodes.items():
            learned = e.get("learned") or {}
            if any(not isinstance(v, dict) for v in (learned.get("buckets") or {}).values()):
                e.pop("learned", None)
                self.dirty = True
                log.info("node %s: gelernte Baseline im alten Format verworfen (ohne Ruhe-Filter gesammelt)",
                         e.get("name") or fp[:16])   # das Register ist nach Fingerprint geschluesselt

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"nodes": self.nodes, "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.dirty, self.last_save = False, time.time()

    def maybe_save(self, now):
        if self.dirty and now - self.last_save > 300:
            try:
                self.save()
            except Exception as e:  # noqa: BLE001
                log.warning("nodes.json speichern: %s", e)

    def by_name(self, name):
        return next(((fp, e) for fp, e in self.nodes.items() if e["name"] == name), (None, None))

    def upsert_hello(self, fp, name, pub_b64, facts, remote):
        """Anmeldung verbuchen: neuer Schluessel -> pending; bekannter -> Fakten/Zeit aktualisieren."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        e = self.nodes.get(fp)
        if e is None:
            base = safe_node_name(name)
            cand, i = base, 2
            while any(x["name"] == cand for x in self.nodes.values()):   # Namenskollision mit anderem Schluessel
                cand, i = f"{base}-{i}", i + 1
            e = {"name": cand, "pubkey": pub_b64, "state": "pending", "facts": {}, "policy": {}, "learned": {},
                 "first_seen": now, "remote": remote}
            self.nodes[fp] = e
            log.info("node %s: neuer Agent-Schluessel %s... von %s -> wartet auf Freigabe", cand, fp[:16], remote)
            state.remember({"event": "node_pending", "node": cand, "fp": fp[:16]})
        e["facts"] = facts or {}
        e["last_seen"], e["remote"], e["pubkey"] = now, remote, pub_b64
        self.dirty = True
        return e


def read_mqtt_password(m):
    """MQTT-Passwort wie der HAPublisher: roh aus der Secrets-Datei, sonst Umgebung/Config."""
    pw = None
    pf = m.get("password_file")
    if pf and os.path.exists(pf):
        key = m.get("password_env", "MQTT_PASSWORD")
        for line in open(pf, encoding="utf-8"):
            if line.startswith(key + "="):
                pw = line.split("=", 1)[1].rstrip("\r\n")
    return pw or os.environ.get(m.get("password_env", "MQTT_PASSWORD")) or m.get("password") or ""


def provision_bundle(e):
    """Konfigurationspaket fuer einen freigegebenen Knoten: was der Agent ausser der Router-URL braucht."""
    pol, facts = e.get("policy") or {}, e.get("facts") or {}
    b = {"heartbeat_interval_s": state.CFG.hb_interval_s, "node": e["name"]}
    if pol.get("mqtt", True) and state.CFG.mqtt and state.CFG.mqtt.get("host"):
        m = state.CFG.mqtt
        b["mqtt"] = {
            "host": m["host"], "port": int(m.get("port") or (8883 if m.get("tls") else 1883)), "tls": bool(m.get("tls")),
            "username": m.get("username", ""), "password": read_mqtt_password(m),
            "device_id": e["name"].replace("-", "_").replace(".", "_"), "device_name": (facts.get("hostname") or e["name"]),
            "manufacturer": facts.get("manufacturer") or "", "model": facts.get("model") or facts.get("gpu") or "",
        }
    return b


def node_spec_from_entry(e):
    """Knoten-Spec aus Fakten (Agent) und Policy (UI); ein gleichnamiger statischer Config-Knoten liefert die Basis."""
    facts, pol = e.get("facts") or {}, e.get("policy") or {}
    spec = dict(state.CFG.nodes.get(e["name"], {}))
    spec.pop("ollama", None)   # registrierte Knoten kommen per Tunnel
    if facts.get("vram_total_mib"):
        spec["vram_total_gib"] = round(facts["vram_total_mib"] / 1024, 1)
    spec["mac"] = pol.get("mac") or facts.get("mac") or spec.get("mac")
    spec["wol"] = bool(pol.get("wol", spec.get("wol", False)))
    spec["weight"] = int(pol.get("weight", spec.get("weight", 1)))
    if pol.get("foreign_vram_baseline_gib") is not None:
        spec["foreign_vram_baseline_gib"] = pol["foreign_vram_baseline_gib"]
    if pol.get("max_inflight") is not None:   # Stufe 3: gleichzeitige Anfragen je Knoten (Admission)
        spec["max_inflight"] = int(pol["max_inflight"])
    for k in ("busy_gpu_util_pct", "busy_foreign_gib"):   # Busy-Schwellen je Knoten (leer = global)
        if pol.get(k) is not None:
            spec[k] = float(pol[k])
    if facts.get("gpu"):
        spec["gpu"] = facts["gpu"]
    return spec


def activate_node(fp):
    """Knotenobjekt fuer einen freigegebenen Registereintrag anlegen oder aktualisieren."""
    e = state.REG.nodes[fp]
    spec = node_spec_from_entry(e)
    node = state.NODES.get(e["name"])
    if node is None:
        node = nodes.Node(e["name"], spec)
        state.NODES[e["name"]] = node
        log.info("node %s: aus dem Register angelegt (Fingerprint %s...)", node.name, fp[:16])
    else:
        node.apply_spec(spec)
    node.fp, node.reg = fp, e
    node.last_known_models |= set(e.get("models") or [])   # zuletzt gesehene Modelle (Register) -> WOL kennt den Knoten auch vor dem ersten Poll
    return node


async def attach_tunnel(node, tun):
    old = node.tunnel
    tun.node, tun.approved = node, True
    node.tunnel = tun
    if old is not None and old is not tun:
        old.close_all("tunnel replaced")
        try:
            await old.ws.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("node %s: Tunnel verbunden von %s", node.name, tun.remote)
    state.remember({"event": "tunnel", "node": node.name, "state": "up"})


async def send_ctl(tun, msg):
    try:
        async with tun.lock:
            await tun.ws.send_json(msg)
    except Exception as e:  # noqa: BLE001
        log.debug("tunnel %s: Steuernachricht: %s", tun.name(), e)


async def approve_node(fp, policy):
    e = state.REG.nodes[fp]
    e["policy"] = {**(e.get("policy") or {}), **{k: v for k, v in policy.items() if v is not None}}
    e["state"] = "approved"
    e["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    node = activate_node(fp)
    state.REG.save()
    state.remember({"event": "node_approved", "node": node.name, "fp": fp[:16]})
    tun = state.PENDING.pop(fp, None)
    if tun is not None:
        await attach_tunnel(node, tun)
        await send_ctl(tun, {"t": "status", "state": "approved", "node": node.name, "config": provision_bundle(e)})
    state.MQTT_DIRTY.append(True)
    if state.HA_PUB is not None:
        try:
            state.HA_PUB.publish_discovery()
        except Exception as ex:  # noqa: BLE001
            log.debug("HA discovery: %s", ex)
    log.info("node %s freigegeben (wol=%s weight=%s mqtt=%s)", node.name, node.wol, node.weight, e["policy"].get("mqtt", True))
    return node


async def revoke_node(fp):
    e = state.REG.nodes[fp]
    e["state"] = "revoked"
    state.REG.save()
    tun = state.PENDING.pop(fp, None)
    node = state.NODES.get(e["name"])
    if node is not None and node.fp == fp:
        if node.tunnel is not None:
            tun = node.tunnel
        if e["name"] not in state.CFG.nodes:     # rein dynamischer Knoten: raus aus dem Routing
            node.tunnel = None
            node.state, node.hot_since, node.calm_since = "offline", None, None
            node.loaded, node.loaded_digest, node.loaded_ctx = {}, {}, {}
            del state.NODES[e["name"]]
        else:
            node.tunnel, node.fp, node.reg = None, None, None
    if tun is not None:
        await send_ctl(tun, {"t": "status", "state": "revoked", "node": e["name"], "message": "in der Router-UI gesperrt"})
        tun.close_all("revoked")
        try:
            await tun.ws.close()
        except Exception:  # noqa: BLE001
            pass
    state.MQTT_DIRTY.append(True)
    state.remember({"event": "node_revoked", "node": e["name"], "fp": fp[:16]})
    log.warning("node %s gesperrt (Fingerprint %s...)", e["name"], fp[:16])


async def push_config(fp):
    e = state.REG.nodes.get(fp)
    node = state.NODES.get(e["name"]) if e else None
    if node is not None and node.tunnel is not None and node.fp == fp:
        await send_ctl(node.tunnel, {"t": "config", "config": provision_bundle(e)})


async def handle_tunnel_v2(request):
    """WebSocket-Endpunkt fuer Agenten mit Schluessel-Identitaet: Challenge -> signiertes Hello -> Status."""
    if Ed25519PublicKey is None:
        return web.json_response({"error": "python3-cryptography fehlt auf dem Router"}, status=503)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)
    nonce = secrets.token_bytes(32)
    await ws.send_json({"t": "challenge", "nonce": base64.b64encode(nonce).decode(), "router": VERSION})
    try:
        msg = await asyncio.wait_for(ws.receive(), 15)
        if msg.type != web.WSMsgType.TEXT:
            raise ValueError("kein Textrahmen")
        hello = json.loads(msg.data)
        if hello.get("t") != "hello":
            raise ValueError("kein hello")
        pub = base64.b64decode(hello["pubkey"])
        sig = base64.b64decode(hello["sig"])
        name = str(hello.get("node") or "")[:64]
        if len(pub) != 32:
            raise ValueError("Public Key hat nicht 32 Bytes")
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, nonce + name.encode() + pub)
    except Exception as e:  # noqa: BLE001
        log.warning("tunnel: Anmeldung von %s abgelehnt: %s", request.remote, e)
        await ws.close(code=4001, message=b"auth failed")
        return ws
    fp = hashlib.sha256(pub).hexdigest()
    facts = hello.get("facts") or {}
    e = state.REG.upsert_hello(fp, name or facts.get("hostname") or fp[:12], base64.b64encode(pub).decode(), facts, request.remote)
    if e["state"] == "revoked":
        await ws.send_json({"t": "status", "state": "revoked", "node": e["name"], "message": "in der Router-UI gesperrt"})
        await ws.close()
        return ws
    tun = tunnel.Tunnel(None, ws, request.remote)
    tun.fp = fp
    if e["state"] == "approved":
        node = activate_node(fp)
        await attach_tunnel(node, tun)
        await ws.send_json({"t": "status", "state": "approved", "node": node.name, "config": provision_bundle(e)})
    else:
        old = state.PENDING.get(fp)
        state.PENDING[fp] = tun
        if old is not None:
            try:
                await old.ws.close()
            except Exception:  # noqa: BLE001
                pass
        state.MQTT_DIRTY.append(True)
        await ws.send_json({"t": "status", "state": "pending", "node": e["name"], "message": "wartet auf Freigabe in der Router-UI"})
    try:
        state.REG.save()
    except Exception as e:  # noqa: BLE001
        log.warning("nodes.json speichern: %s", e)
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                tun.dispatch(msg.data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE, web.WSMsgType.CLOSING):
                break
    finally:
        tun.close_all("tunnel closed")
        if state.PENDING.get(fp) is tun:
            del state.PENDING[fp]
            state.MQTT_DIRTY.append(True)
        node = tun.node
        if node is not None and node.tunnel is tun:
            node.tunnel = None
            log.warning("node %s: Tunnel getrennt", node.name)
            state.remember({"event": "tunnel", "node": node.name, "state": "down"})
            if not node.url and node.state != "offline":
                node.state, node.hot_since, node.calm_since = "offline", None, None
                node.loaded, node.loaded_digest, node.loaded_ctx = {}, {}, {}
                state.MQTT_DIRTY.append(True)
                log.warning("node %s -> offline (Tunnel weg)", node.name)
    return ws


def registry_view():
    out = []
    for fp, e in state.REG.nodes.items():
        node = state.NODES.get(e["name"])
        out.append({"fp": fp, "name": e["name"], "state": e["state"], "facts": e.get("facts") or {}, "policy": e.get("policy") or {},
                    "learned": e.get("learned") or {}, "models": e.get("models") or [],   # zuletzt gesehene Modelle (fuer WOL nach Neustart)
                    "first_seen": e.get("first_seen"), "last_seen": e.get("last_seen"), "remote": e.get("remote"),
                    "approved_at": e.get("approved_at"),
                    "connected": (fp in state.PENDING) or (node is not None and node.fp == fp and node.tunnel is not None),
                    "node_state": node.state if (node is not None and node.fp == fp) else None})
    return out


async def handle_nodes(request):
    return web.json_response({"nodes": registry_view(), "pending": [x["name"] for x in registry_view() if x["state"] == "pending"]})


async def handle_node_action(request):
    fp, action = request.match_info["fp"], request.match_info["action"]
    if fp not in state.REG.nodes:
        return web.json_response({"error": "unbekannter Fingerprint"}, status=404)
    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:  # noqa: BLE001
        body = {}
    pol = {k: body.get(k) for k in ("wol", "weight", "mqtt", "foreign_vram_baseline_gib", "mac", "max_inflight", "busy_gpu_util_pct", "busy_foreign_gib") if k in body}
    for k, lo, hi in (("busy_gpu_util_pct", 1, 100), ("busy_foreign_gib", 0.1, 64)):
        if pol.get(k) is not None:
            try:
                pol[k] = float(pol[k])
            except (TypeError, ValueError):
                return web.json_response({"error": f"{k}: Zahl erwartet"}, status=400)
            if not lo <= pol[k] <= hi:
                return web.json_response({"error": f"{k}: {pol[k]} liegt nicht zwischen {lo} und {hi}"}, status=400)
    if "weight" in pol and pol["weight"] is not None:
        pol["weight"] = int(pol["weight"])
    if pol.get("max_inflight") is not None:
        pol["max_inflight"] = int(pol["max_inflight"])
    if action == "approve":
        node = await approve_node(fp, pol)
        return web.json_response({"ok": True, "node": node.name})
    if action == "revoke":
        await revoke_node(fp)
        return web.json_response({"ok": True})
    if action == "policy":
        e = state.REG.nodes[fp]
        e["policy"] = {**(e.get("policy") or {}), **{k: v for k, v in pol.items() if v is not None}}
        for k, v in pol.items():
            if v is None:
                e["policy"].pop(k, None)
        state.REG.save()
        if e["state"] == "approved":
            activate_node(fp)
            await push_config(fp)
        return web.json_response({"ok": True, "policy": e["policy"]})
    if action == "rename":
        new = safe_node_name(body.get("name") or "")
        e = state.REG.nodes[fp]
        if not new or any(x["name"] == new for f2, x in state.REG.nodes.items() if f2 != fp) or (new in state.CFG.nodes and new != e["name"]):
            return web.json_response({"error": "Name leer oder schon vergeben"}, status=400)
        if e["state"] == "approved":
            return web.json_response({"error": "Umbenennen nur vor der Freigabe (Rollen/Perf haengen am Namen)"}, status=400)
        e["name"] = new
        state.REG.save()
        return web.json_response({"ok": True, "name": new})
    return web.json_response({"error": "unbekannte Aktion"}, status=404)


async def handle_node_delete(request):
    fp = request.match_info["fp"]
    e = state.REG.nodes.get(fp)
    if e is None:
        return web.json_response({"error": "unbekannter Fingerprint"}, status=404)
    if e["state"] == "approved":
        return web.json_response({"error": "erst sperren, dann loeschen"}, status=400)
    tun = state.PENDING.pop(fp, None)
    if tun is not None:
        try:
            await tun.ws.close()
        except Exception:  # noqa: BLE001
            pass
    del state.REG.nodes[fp]
    state.REG.save()
    state.MQTT_DIRTY.append(True)
    return web.json_response({"ok": True})


async def handle_tunnel(request):
    """Alter WebSocket-Endpunkt mit Token (Agenten vor 0.4.0). Ohne konfigurierten Token abgeschaltet."""
    if not state.CFG.heartbeat_token:
        return web.json_response({"error": "Token-Tunnel abgeschaltet, Agent >= 0.4.0 nutzt /v1/tunnel mit Schluessel"}, status=410)
    if request.headers.get("X-Router-Token") != state.CFG.heartbeat_token:
        return web.json_response({"error": "bad token"}, status=401)
    name = request.match_info["node"]
    node = state.NODES.get(name)
    if node is None:
        return web.json_response({"error": f"unknown node {name}"}, status=404)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)
    old, tun = node.tunnel, tunnel.Tunnel(node, ws, request.remote)
    node.tunnel = tun
    if old is not None:
        old.close_all("tunnel replaced")
        try:
            await old.ws.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("node %s: Tunnel verbunden von %s", name, request.remote)
    state.remember({"event": "tunnel", "node": name, "state": "up"})
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                tun.dispatch(msg.data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE, web.WSMsgType.CLOSING):
                break
    finally:
        tun.close_all("tunnel closed")
        if node.tunnel is tun:
            node.tunnel = None
            log.warning("node %s: Tunnel getrennt", name)
            state.remember({"event": "tunnel", "node": name, "state": "down"})
            if not node.url and node.state != "offline":
                node.state, node.hot_since, node.calm_since = "offline", None, None
                node.loaded, node.loaded_digest, node.loaded_ctx = {}, {}, {}
                state.MQTT_DIRTY.append(True)
                log.warning("node %s -> offline (Tunnel weg)", name)
    return ws
