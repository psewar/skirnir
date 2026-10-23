"""Node registry with key identity: registration, release, locking, provisioning."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import secrets
import time

from aiohttp import web

if importlib.util.find_spec("cryptography") is not None:   # Debian: python3-cryptography
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
else:  # pragma: no cover
    InvalidSignature = Ed25519PublicKey = None

from . import agentupdate, nodes, ollamaupdate, state, tunnel, values
from .common import BASE64, VERSION, log, normalize_mac, read_env_value, read_json, safe_node_name
from .outcome import Problem, failed, invalid, unauthenticated


class NodeRegistry:
    def __init__(self, path):
        self.path = path
        self.nodes = {}
        self.dirty = False
        self.last_save = 0.0

    def load(self):
        self.nodes = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:  # exception-ok: file system and the file's JSON (JSONDecodeError, UnicodeDecodeError)
                log.error("nodes.json unreadable (%s) - starting with an empty register", e)
                data = {}
            nodes_ = data.get("nodes") if isinstance(data, dict) else None
            if isinstance(nodes_, dict):
                self.nodes = nodes_
            elif data:
                log.error("nodes.json has no mapping 'nodes' - starting with an empty register")
        self.drop_stale_learned()
        return self

    def drop_stale_learned(self):
        """One-time on startup: discard learned data in the old format (bucket = a number instead of {n,sum,min}).

        It was created without the idle filter and is systematically corrupted: learning only happens when Ollama holds nothing
        - on a node with a persistently warm model, that is almost only during a game, because only
        `unload_on_busy` removes the model from VRAM. The learner thus saw the world mostly in the game state.
        Discard instead of converting; until enough idle samples are available, the policy value carries the load."""
        for fp, e in self.nodes.items():
            learned = e.get("learned") or {}
            if any(not isinstance(v, dict) for v in (learned.get("buckets") or {}).values()):
                e.pop("learned", None)
                self.dirty = True
                log.info("node %s: learned baseline in the old format dropped (collected without the calm filter)",
                         e.get("name") or fp[:16])   # the registry is keyed by fingerprint

    def save(self):
        """Write registry (atomic). If the filesystem fails, the in-memory state remains valid and dirty - maybe_save
        will retry; the caller receives the Problem."""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"nodes": self.nodes, "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:  # exception-ok: file system boundary
            log.warning("nodes.json save: %s", e)
            self.dirty = True
            return Problem(500, "nodes.json not writable")
        self.dirty, self.last_save = False, time.time()
        return None

    def maybe_save(self, now):
        if self.dirty and now - self.last_save > 300:
            self.save()

    def by_name(self, name):
        return next(((fp, e) for fp, e in self.nodes.items() if e["name"] == name), (None, None))

    def upsert_hello(self, fp, name, pub_b64, facts, remote):
        """Record registration: new key -> pending; known -> update facts/time."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        e = self.nodes.get(fp)
        if e is None:
            base = safe_node_name(name)
            cand, i = base, 2
            while any(x["name"] == cand for x in self.nodes.values()):   # Name collision with a different key
                cand, i = f"{base}-{i}", i + 1
            e = {"name": cand, "pubkey": pub_b64, "state": "pending", "facts": {}, "policy": {}, "learned": {},
                 "first_seen": now, "remote": remote}
            self.nodes[fp] = e
            log.info("node %s: new agent key %s... from %s -> awaiting approval", cand, fp[:16], remote)
            state.remember({"event": "node_pending", "node": cand, "fp": fp[:16]})
        agentupdate.note_hello(e, facts or {})   # Versionswechsel merken, offenen Update-Auftrag abschliessen
        ollamaupdate.note_hello(e, facts or {})  # Ollama version from the registration, if the registry does not know it yet
        e["facts"] = facts or {}
        e["last_seen"], e["remote"], e["pubkey"] = now, remote, pub_b64
        self.dirty = True
        return e


def read_mqtt_password(m):
    """MQTT password like HAPublisher: raw from the secrets file, otherwise environment/config."""
    key = m.get("password_env", "MQTT_PASSWORD")
    pw = read_env_value(m["password_file"], key) if m.get("password_file") else None
    return pw or os.environ.get(key) or m.get("password") or ""


def provision_bundle(e):
    """Configuration package for a released node: what the agent needs besides the router URL."""
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
    """Node spec from facts (agent) and policy (UI); a static config node with the same name provides the base."""
    facts, pol = e.get("facts") or {}, e.get("policy") or {}
    spec = dict(state.CFG.nodes.get(e["name"], {}))
    spec.pop("ollama", None)   # registered nodes come via tunnel
    if facts.get("vram_total_mib"):
        spec["vram_total_gib"] = round(facts["vram_total_mib"] / 1024, 1)
    spec["mac"] = pol.get("mac") or facts.get("mac") or spec.get("mac")
    spec["wol"] = bool(pol.get("wol", spec.get("wol", False)))
    spec["weight"] = int(pol.get("weight", spec.get("weight", 1)))
    if pol.get("foreign_vram_baseline_gib") is not None:
        spec["foreign_vram_baseline_gib"] = pol["foreign_vram_baseline_gib"]
    if pol.get("max_inflight") is not None:   # Stage 3: concurrent requests per node (admission)
        spec["max_inflight"] = int(pol["max_inflight"])
    for k in ("busy_gpu_util_pct", "busy_foreign_gib"):   # Busy thresholds per node (empty = global)
        if pol.get(k) is not None:
            spec[k] = float(pol[k])
    if pol.get("gpu_guard") is not None:   # Policy: respect GPU guard status of this node (False e.g. for cards without 12V-2x6)
        spec["gpu_guard"] = bool(pol["gpu_guard"])
    if facts.get("gpu"):
        spec["gpu"] = facts["gpu"]
    if facts.get("upstream_api") == "openai":   # Agent fronts an OpenAI-compatible server (openai_node.py); else Ollama
        spec["upstream_api"] = "openai"
        if facts.get("upstream_release") is True:   # ... and can stop/start it on order (agent >= 0.22.0)
            spec["upstream_releasable"] = True
    return spec


def activate_node(fp):
    """Create or update node object for a released registry entry."""
    e = state.REG.nodes[fp]
    spec = node_spec_from_entry(e)
    node = state.NODES.get(e["name"])
    if node is None:
        node = nodes.Node(e["name"], spec)
        state.NODES[e["name"]] = node
        log.info("node %s: created from the register (fingerprint %s...)", node.name, fp[:16])
    else:
        node.apply_spec(spec)
    node.fp, node.reg = fp, e
    node.last_known_models |= set(e.get("models") or [])   # last seen models (registry) -> WOL knows the node even before the first poll
    return node


async def close_ws(ws):
    """Close WebSocket of a detached or locked tunnel; if the connection is already gone, there is nothing to do."""
    if ws.closed:
        return
    try:
        await ws.close()
    except Exception as e:  # noqa: BLE001  # exception-ok: network boundary, the peer may vanish while closing
        log.debug("tunnel: close: %s", e)


async def attach_tunnel(node, tun):
    old = node.tunnel
    tun.node, tun.approved = node, True
    node.tunnel = tun
    if old is not None and old is not tun:
        old.close_all("tunnel replaced")
        await close_ws(old.ws)
    log.info("node %s: tunnel connected from %s", node.name, tun.remote)
    state.remember({"event": "tunnel", "node": node.name, "state": "up"})


async def send_ctl(tun, msg):
    try:
        async with tun.lock:
            await tun.ws.send_json(msg)
    except Exception as e:  # noqa: BLE001  # exception-ok: network boundary; a lost control message is resent on the next hello
        log.debug("tunnel %s: control message: %s", tun.name(), e)


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
        except Exception as ex:  # noqa: BLE001  # exception-ok: MQTT broker boundary; the next publish cycle repeats discovery
            log.debug("HA discovery: %s", ex)
    log.info("node %s approved (wol=%s weight=%s mqtt=%s)", node.name, node.wol, node.weight, e["policy"].get("mqtt", True))
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
        if e["name"] not in state.CFG.nodes:     # purely dynamic node: remove from routing
            node.tunnel = None
            node.go_offline("blocked")
            del state.NODES[e["name"]]
        else:
            node.tunnel, node.fp, node.reg = None, None, None
    if tun is not None:
        await send_ctl(tun, {"t": "status", "state": "revoked", "node": e["name"], "message": "revoked in the router UI"})
        tun.close_all("revoked")
        await close_ws(tun.ws)
    state.MQTT_DIRTY.append(True)
    state.remember({"event": "node_revoked", "node": e["name"], "fp": fp[:16]})
    log.warning("node %s revoked (fingerprint %s...)", e["name"], fp[:16])


async def push_config(fp):
    e = state.REG.nodes.get(fp)
    node = state.NODES.get(e["name"]) if e else None
    if node is not None and node.tunnel is not None and node.fp == fp:
        await send_ctl(node.tunnel, {"t": "config", "config": provision_bundle(e)})


async def receive_hello(ws, nonce, timeout=15):
    """Verify signed Hello of the agent: (pub, name, hello) or a Problem 401 with the reason."""
    recv = asyncio.ensure_future(ws.receive())
    done, _ = await asyncio.wait({recv}, timeout=timeout)
    if not done:
        recv.cancel()
        return unauthenticated(f"no hello within {timeout} s")
    msg = recv.result()
    if msg.type != web.WSMsgType.TEXT:
        return unauthenticated("not a text frame")
    try:
        hello = json.loads(msg.data)
    except ValueError:  # exception-ok: untrusted JSON from the agent
        return unauthenticated("not JSON")
    if not isinstance(hello, dict) or hello.get("t") != "hello":
        return unauthenticated("not a hello")
    if not all(isinstance(hello.get(k), str) and BASE64.fullmatch(hello[k]) for k in ("pubkey", "sig")):
        return unauthenticated("pubkey/sig are not base64")
    pub, sig = base64.b64decode(hello["pubkey"]), base64.b64decode(hello["sig"])
    name = str(hello.get("node") or "")[:64]
    if len(pub) != 32:
        return unauthenticated("public key is not 32 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, nonce + name.encode() + pub)
    except (InvalidSignature, ValueError):  # exception-ok: cryptography reports a wrong signature or key only as an exception
        return unauthenticated("signature does not match")
    return pub, name, hello


async def handle_tunnel_v2(request):
    """WebSocket endpoint for agents with key identity: Challenge -> signed Hello -> Status."""
    if Ed25519PublicKey is None:
        return web.json_response({"error": "python3-cryptography is missing on the router"}, status=503)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)
    nonce = secrets.token_bytes(32)
    await ws.send_json({"t": "challenge", "nonce": base64.b64encode(nonce).decode(), "router": VERSION})
    hello = await receive_hello(ws, nonce)
    if failed(hello):
        log.warning("tunnel: sign-in from %s refused: %s", request.remote, hello.message)
        await ws.close(code=4001, message=b"auth failed")
        return ws
    pub, name, hello = hello
    fp = hashlib.sha256(pub).hexdigest()
    facts = hello.get("facts") if isinstance(hello.get("facts"), dict) else {}
    e = state.REG.upsert_hello(fp, name or facts.get("hostname") or fp[:12], base64.b64encode(pub).decode(), facts, request.remote)
    if e["state"] == "revoked":
        await ws.send_json({"t": "status", "state": "revoked", "node": e["name"], "message": "revoked in the router UI"})
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
            await close_ws(old.ws)
        state.MQTT_DIRTY.append(True)
        await ws.send_json({"t": "status", "state": "pending", "node": e["name"], "message": "awaiting approval in the router UI"})
    state.REG.save()   # if it fails, the registry remains dirty and maybe_save will retry
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
            node.ollama_push_ts = 0.0   # no tunnel, no messages; after reconnecting, poll again until the first message
            log.warning("node %s: tunnel disconnected", node.name)
            state.remember({"event": "tunnel", "node": node.name, "state": "down"})
            if not node.url and node.state != "offline":
                node.go_offline("tunnel gone")
    return ws


def registry_view():
    out = []
    for fp, e in state.REG.nodes.items():
        node = state.NODES.get(e["name"])
        out.append({"fp": fp, "name": e["name"], "state": e["state"], "facts": e.get("facts") or {}, "policy": e.get("policy") or {},
                    "learned": e.get("learned") or {}, "models": e.get("models") or [],   # last seen models (for WOL after restart)
                    "first_seen": e.get("first_seen"), "last_seen": e.get("last_seen"), "remote": e.get("remote"),
                    "approved_at": e.get("approved_at"), "update": agentupdate.update_view(e), "ollama_update": ollamaupdate.update_view(e),
                    "connected": (fp in state.PENDING) or (node is not None and node.fp == fp and node.tunnel is not None),
                    "node_state": node.state if (node is not None and node.fp == fp) else None})
    return out


async def handle_nodes(request):
    return web.json_response({"nodes": registry_view(), "pending": [x["name"] for x in registry_view() if x["state"] == "pending"]})


POLICY_KEYS = ("wol", "weight", "mqtt", "foreign_vram_baseline_gib", "mac", "max_inflight", "busy_gpu_util_pct", "busy_foreign_gib",
               "gpu_guard", "auto_update", "ollama_auto_update")


def node_policy(body):
    """Policy fields from the UI: the set fields (None = remove) or a Problem 400."""
    pol = {k: body.get(k) for k in POLICY_KEYS if k in body}
    for k, lo, hi in (("busy_gpu_util_pct", 1, 100), ("busy_foreign_gib", 0.1, 64)):
        if pol.get(k) is not None:
            pol[k] = values.number(pol[k], k)
            if failed(pol[k]):
                return pol[k]
            if not lo <= pol[k] <= hi:
                return invalid(f"{k}: {pol[k]} is not between {lo} and {hi}")
    for k in ("weight", "max_inflight"):
        if pol.get(k) is not None:
            pol[k] = values.number(pol[k], k, integer=True)
            if failed(pol[k]):
                return pol[k]
    if pol.get("mac") is not None:
        pol["mac"] = normalize_mac(pol["mac"])
        if pol["mac"] is None:
            return invalid("mac: six hex pairs expected (aa:bb:cc:dd:ee:ff)")
    return pol


async def handle_node_action(request):
    fp, action = request.match_info["fp"], request.match_info["action"]
    if fp not in state.REG.nodes:
        return web.json_response({"error": "unknown fingerprint"}, status=404)
    body = (await read_json(request) if request.can_read_body else None) or {}
    if not isinstance(body, dict):
        return web.json_response({"error": "object expected"}, status=400)
    pol = node_policy(body)
    if failed(pol):
        return web.json_response({"error": pol.message}, status=pol.status)
    if action == "approve":
        node = await approve_node(fp, pol)
        return web.json_response({"ok": True, "node": node.name})
    if action == "revoke":
        await revoke_node(fp)
        return web.json_response({"ok": True})
    if action == "update":   # Trigger agent update (agentupdate.py); force = also with running requests / same version
        ok, msg = await agentupdate.order_update(fp, "ui", force=bool(body.get("force")))
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 409)
    if action == "ollama-update":   # Ollama-Update anstossen (ollamaupdate.py, 0.3.0)
        ok, msg = await ollamaupdate.order_update(fp, "ui", force=bool(body.get("force")))
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 409)
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
            return web.json_response({"error": "name empty or already taken"}, status=400)
        if e["state"] == "approved":
            return web.json_response({"error": "rename only before approval (roles and perf data are keyed by name)"}, status=400)
        e["name"] = new
        state.REG.save()
        return web.json_response({"ok": True, "name": new})
    return web.json_response({"error": "unknown action"}, status=404)


async def handle_node_delete(request):
    fp = request.match_info["fp"]
    e = state.REG.nodes.get(fp)
    if e is None:
        return web.json_response({"error": "unknown fingerprint"}, status=404)
    if e["state"] == "approved":
        return web.json_response({"error": "revoke first, then delete"}, status=400)
    tun = state.PENDING.pop(fp, None)
    if tun is not None:
        await close_ws(tun.ws)
    del state.REG.nodes[fp]
    state.REG.save()
    state.MQTT_DIRTY.append(True)
    return web.json_response({"ok": True})

