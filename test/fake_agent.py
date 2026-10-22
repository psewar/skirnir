#!/usr/bin/env python3
"""Fake agent for tests: registers with the router using an Ed25519 key like agent/tunnel.go (Challenge ->
signed Hello -> Status), maintains the tunnel, and forwards router requests to a (fake) Ollama.
The key is persisted in <keyfile> so a restart retains the same identity (like the real agent).
Usage: fake_agent.py <router-ws> <node> <upstream> <keyfile> [upstream-token]
  e.g. fake_agent.py wss://127.0.0.1:21435 big https://127.0.0.1:21001 fake-agent-big.key testtoken"""
import asyncio
import base64
import json
import os
import ssl
import sys
import time

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROUTER_WS, NODE, UPSTREAM, KEYFILE = sys.argv[1:5]
UP_TOKEN = sys.argv[5] if len(sys.argv) > 5 else None
REQ, RESP, DATA, END, ERR, CANCEL, HB, HBACK, OLLAMA = 1, 2, 3, 4, 5, 6, 7, 8, 9
FACTS = {"hostname": NODE.upper(), "os": "windows", "arch": "amd64", "manufacturer": "Fake", "model": "Testrechner",
         "gpu": "Fake RTX 5090", "vram_total_mib": 32563, "mac": "00:11:22:33:44:55", "local_ip": "127.0.0.1",
         "ollama_version": "0.11.0", "ollama_url": UPSTREAM, "agent_version": "test",
         "ollama_managed": os.environ.get("FAKE_OLLAMA_MANAGED", "1") == "1",   # like Agent 0.12.1: Ollama is a child of the agent
         "ollama_push": os.environ.get("FAKE_OLLAMA_PUSH", "1") == "1",         # like Agent 0.14.0: reports Ollama status itself
         "ollama_swap_gate": os.environ.get("FAKE_OLLAMA_SWAP_GATE", "1") == "1"}   # like Agent 0.15.0: swap only after release
SWAP_GO = {}   # Version -> asyncio.Event, gesetzt von {"t":"ollama-swap"}


def load_key():
    if os.path.exists(KEYFILE):
        return Ed25519PrivateKey.from_private_bytes(open(KEYFILE, "rb").read())
    k = Ed25519PrivateKey.generate()
    raw = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    open(KEYFILE, "wb").write(raw)
    return k


KEY = load_key()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


UPDATE_PUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-update.pub")   # base64, von run_tests geschrieben


async def do_update(ws, session, lock, m, sslctx):
    """Like updater.go: signature with the operator key, file in manifest, download with token, SHA-256, report in
    heartbeat, then re-register with the new version (swap and restart are only simulated)."""
    import hashlib
    hb = {"gpu_util_pct": 0, "vram_total_mib": 32563, "vram_used_mib": 7200, "vram_free_mib": 25363}
    async def report(state, msg=""):   # child like Agent >= 0.19.1: always present, never app_control here
        await send(ws, lock, HB, 0, json.dumps({**hb, "update": {"version": m.get("version"), "state": state, "message": msg, "kind": ""}}).encode())
        out(update=state, msg=msg)
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(open(UPDATE_PUB, encoding="utf-8").read().strip()))
        pub.verify(base64.b64decode(m["signature"]), m["manifest"].encode("utf-8"))
        man = json.loads(m["manifest"])
        f = next(x for x in man["files"] if x["name"] == m["file"] and x["os"] == FACTS["os"] and x["arch"] == FACTS["arch"])
        if f["sha256"] != m["sha256"] or man["version"] != m["version"]:
            raise ValueError("Auftrag widerspricht Manifest")
        await report("downloading")
        async with session.get(m["url"], headers={"Authorization": "Bearer " + m["token"]}, ssl=sslctx) as r:
            data = await r.read()
            if r.status != 200:
                raise ValueError(f"Download HTTP {r.status}")
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            raise ValueError("SHA-256 stimmt nicht")
        await report("applied", "Binary getauscht, Neustart")
        FACTS["agent_version"] = m["version"]
        await asyncio.sleep(0.3)
        await ws.close()   # 'Restart': the loop re-registers with the new version
    except Exception as e:  # noqa: BLE001
        await report("failed", (str(e) or e.__class__.__name__.replace("InvalidSignature", "Manifest-Signatur ungueltig"))[:120])


async def do_ollama_update(ws, session, lock, m):
    """Like ollamaupdate.go (simplified): load archive, SHA-256 against the task, status in heartbeat; then the
    heartbeat carries the new Ollama version (stopping, swapping, and restarting the child are only simulated)."""
    import hashlib
    hb = {"gpu_util_pct": 0, "vram_total_mib": 32563, "vram_used_mib": 7200, "vram_free_mib": 25363}
    async def report(state, msg=""):
        await send(ws, lock, HB, 0, json.dumps({**hb, "ollama_version": FACTS["ollama_version"],
                                                "ollama_update": {"version": m.get("version"), "state": state, "message": msg}}).encode())
        out(ollama_update=state, msg=msg)
    try:
        if not FACTS["ollama_managed"]:   # like ollamaupdate.go on a node with Ollama as a tray app
            await report("failed", "Ollama ist kein Kind dieses Agenten (children: ohne ollama.exe) - Update nur von Hand")
            return
        await report("downloading", "0 %")
        async with session.get(m["url"]) as r:
            data = await r.read()
            if r.status != 200:
                raise ValueError(f"Download HTTP {r.status}")
        await asyncio.sleep(1.5)   # "Download takes time": the node must stay in routing during this period
        if hashlib.sha256(data).hexdigest() != m["sha256"]:
            raise ValueError("SHA-256 stimmt nicht")
        if FACTS["ollama_swap_gate"]:
            go = SWAP_GO.setdefault(m["version"], asyncio.Event())
            await report("ready", "geladen und geprueft - wartet auf die Freigabe des Routers")
            try:
                await asyncio.wait_for(go.wait(), 20)
            except asyncio.TimeoutError:
                await report("failed", "Router gab den Tausch nicht frei")
                return
        await report("applied", "Dateien getauscht, Ollama startet neu")
        FACTS["ollama_version"] = m["version"]
        await asyncio.sleep(0.3)
        await report("applied", "Dateien getauscht, Ollama startet neu")   # next heartbeat carries the new version
    except Exception as e:  # noqa: BLE001
        await report("failed", (str(e) or e.__class__.__name__)[:120])


async def ollama_watch(ws, session, lock, watch):
    """Like ollamawatch.go (Agent 0.14.0): query local Ollama every 0.5 s and report status on every change, after
    release, and every 5 s as a frame OLLAMA; after two consecutive failures up=false (Agent: three, the test is tighter)."""
    headers = {"X-Router-Token": UP_TOKEN} if UP_TOKEN else {}
    tmo = aiohttp.ClientTimeout(total=2)
    fails, key, sent, msg = 0, None, 0.0, None
    while True:
        try:
            async with session.get(UPSTREAM + "/api/ps", headers=headers, ssl=False, timeout=tmo) as r:
                ps = (await r.json()).get("models", [])
            async with session.get(UPSTREAM + "/api/tags", headers=headers, ssl=False, timeout=tmo) as r:
                tags = (await r.json()).get("models", [])
            fails, msg = 0, {"up": True, "tags": tags, "ps": ps}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  - Netzwerkfehler lassen sich nicht vorab pruefen
            fails += 1
            if fails >= 2:
                msg = {"up": False, "error": (str(e) or e.__class__.__name__)[:120]}
        if msg is not None:
            k = json.dumps({**msg, "ps": [{a: b for a, b in m.items() if a != "expires_at"} for m in msg.get("ps", [])]}, sort_keys=True)
            if watch["force"] or k != key or time.time() - sent >= 5:
                await send(ws, lock, OLLAMA, 0, json.dumps({**msg, "ts": int(time.time())}).encode())
                if k != key:
                    out(ollama="up" if msg["up"] else "down")
                key, sent, watch["force"] = k, time.time(), False
        await asyncio.sleep(0.5)


def out(**kw):
    print(json.dumps({"agent": NODE, **kw}), flush=True)


async def send(ws, lock, t, sid, payload=b""):
    async with lock:
        await ws.send_bytes(bytes([t]) + sid.to_bytes(4, "big") + payload)


async def handle(ws, session, sid, payload, tasks, lock):
    try:
        i = payload.index(b"\n")
        head = json.loads(payload[:i])
        body = payload[i + 1:]
        headers = dict(head.get("headers") or {})
        if UP_TOKEN:
            headers["X-Router-Token"] = UP_TOKEN
        async with session.request(head["method"], UPSTREAM + head["path"], data=body, headers=headers, ssl=False) as r:
            await send(ws, lock, RESP, sid, json.dumps({"status": r.status, "headers": dict(r.headers)}).encode())
            async for chunk in r.content.iter_any():
                await send(ws, lock, DATA, sid, chunk)
            await send(ws, lock, END, sid)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        try:
            await send(ws, lock, ERR, sid, str(e).encode())
        except Exception:  # noqa: BLE001
            pass
    finally:
        tasks.pop(sid, None)


async def main():
    sslctx = ssl.create_default_context()
    sslctx.check_hostname = False
    sslctx.verify_mode = ssl.CERT_NONE
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"{ROUTER_WS}/v1/tunnel", ssl=sslctx, heartbeat=20) as ws:
                    ch = json.loads((await ws.receive_str()))
                    nonce = base64.b64decode(ch["nonce"])
                    sig = KEY.sign(nonce + NODE.encode() + PUB)
                    await ws.send_json({"t": "hello", "node": NODE, "pubkey": base64.b64encode(PUB).decode(),
                                        "sig": base64.b64encode(sig).decode(), "facts": FACTS})
                    st = json.loads(await ws.receive_str())
                    out(tunnel="up", status=st.get("state"), node=st.get("node"), config=st.get("config"))
                    tasks, lock = {}, asyncio.Lock()
                    watch = {"force": True}
                    wtask = asyncio.create_task(ollama_watch(ws, session, lock, watch)) if FACTS["ollama_push"] else None
                    if st.get("state") == "approved":   # one heartbeat through the tunnel (idle values, 7 GiB = baseline)
                        await send(ws, lock, HB, 0, json.dumps({"gpu_util_pct": 0, "vram_total_mib": 32563, "vram_used_mib": 7200, "vram_free_mib": 25363}).encode())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            m = json.loads(msg.data)
                            out(ctl=m.get("t"), status=m.get("state"), node=m.get("node"), config=m.get("config"))
                            if m.get("state") == "approved":
                                watch["force"] = True   # before release, the router discards the frame
                                await send(ws, lock, HB, 0, json.dumps({"gpu_util_pct": 0, "vram_total_mib": 32563, "vram_used_mib": 7200, "vram_free_mib": 25363}).encode())
                            if m.get("t") == "update":
                                asyncio.create_task(do_update(ws, session, lock, m, sslctx))  # noqa: RUF006
                            if m.get("t") == "ollama-update":
                                asyncio.create_task(do_ollama_update(ws, session, lock, m))  # noqa: RUF006
                            if m.get("t") == "ollama-swap":
                                SWAP_GO.setdefault(m.get("version"), asyncio.Event()).set()
                            continue
                        if msg.type != aiohttp.WSMsgType.BINARY:
                            continue
                        t, sid, payload = msg.data[0], int.from_bytes(msg.data[1:5], "big"), msg.data[5:]
                        if t == REQ:
                            tasks[sid] = asyncio.create_task(handle(ws, session, sid, payload, tasks, lock))
                        elif t == CANCEL and sid in tasks:
                            tasks[sid].cancel()
                        elif t == HBACK:
                            out(hback=json.loads(payload))
                    if wtask is not None:
                        wtask.cancel()
                    for tk in list(tasks.values()):
                        tk.cancel()
            out(tunnel="down")
        except Exception as e:  # noqa: BLE001
            out(tunnel="error", err=str(e)[:120])
        await asyncio.sleep(0.5)


asyncio.run(main())
