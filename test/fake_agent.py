#!/usr/bin/env python3
"""Fake-Agent fuer die Tests: meldet sich wie agent-go/tunnel.go mit Ed25519-Schluessel am Router an (Challenge ->
signiertes Hello -> Status), haelt den Tunnel und reicht die Anfragen des Routers an ein (Fake-)Ollama weiter.
Schluessel wird in <keyfile> persistiert, damit ein Neustart dieselbe Identitaet hat (wie der echte Agent).
Aufruf: fake_agent.py <router-ws> <node> <upstream> <keyfile> [upstream-token]
  z. B. fake_agent.py wss://127.0.0.1:21435 big https://127.0.0.1:21001 fake-agent-big.key testtoken"""
import asyncio
import base64
import json
import os
import ssl
import sys

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROUTER_WS, NODE, UPSTREAM, KEYFILE = sys.argv[1:5]
UP_TOKEN = sys.argv[5] if len(sys.argv) > 5 else None
REQ, RESP, DATA, END, ERR, CANCEL, HB, HBACK = 1, 2, 3, 4, 5, 6, 7, 8
FACTS = {"hostname": NODE.upper(), "os": "windows", "arch": "amd64", "manufacturer": "Fake", "model": "Testrechner",
         "gpu": "Fake RTX 5090", "vram_total_mib": 32563, "mac": "00:11:22:33:44:55", "local_ip": "127.0.0.1",
         "ollama_version": "0.11.0", "ollama_url": UPSTREAM, "agent_version": "test"}


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
    """Wie updater.go: Signatur mit dem Betreiber-Schluessel, Datei im Manifest, Download mit Token, SHA-256, Bericht im
    Heartbeat, dann Neuanmeldung mit der neuen Version (Tausch und Neustart werden nur simuliert)."""
    import hashlib
    hb = {"gpu_util_pct": 0, "vram_total_mib": 32563, "vram_used_mib": 7200, "vram_free_mib": 25363}
    async def report(state, msg=""):
        await send(ws, lock, HB, 0, json.dumps({**hb, "update": {"version": m.get("version"), "state": state, "message": msg}}).encode())
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
        await ws.close()   # 'Neustart': die Schleife meldet sich mit der neuen Version wieder an
    except Exception as e:  # noqa: BLE001
        await report("failed", (str(e) or e.__class__.__name__.replace("InvalidSignature", "Manifest-Signatur ungueltig"))[:120])


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
                    if st.get("state") == "approved":   # ein Heartbeat durch den Tunnel (Leerlaufwerte, 7 GiB = Baseline)
                        await send(ws, lock, HB, 0, json.dumps({"gpu_util_pct": 0, "vram_total_mib": 32563, "vram_used_mib": 7200, "vram_free_mib": 25363}).encode())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            m = json.loads(msg.data)
                            out(ctl=m.get("t"), status=m.get("state"), node=m.get("node"), config=m.get("config"))
                            if m.get("state") == "approved":
                                await send(ws, lock, HB, 0, json.dumps({"gpu_util_pct": 0, "vram_total_mib": 32563, "vram_used_mib": 7200, "vram_free_mib": 25363}).encode())
                            if m.get("t") == "update":
                                asyncio.create_task(do_update(ws, session, lock, m, sslctx))  # noqa: RUF006
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
                    for tk in list(tasks.values()):
                        tk.cancel()
            out(tunnel="down")
        except Exception as e:  # noqa: BLE001
            out(tunnel="error", err=str(e)[:120])
        await asyncio.sleep(0.5)


asyncio.run(main())
