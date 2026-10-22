#!/usr/bin/env python3
"""Self-test Tunnel.open: an aborted caller leaves no stream, and the agent receives CANCEL.

Trigger: If the coroutine awaiting the response header was aborted externally (client disconnects during prefill,
external timeout like asyncio.wait_for or asyncio.timeout), the entry in `tunnel.streams` remained until tunnel end,
and the agent finished processing the request. Runs without router, network, or agent: a WebSocket mock receives
frames and responds according to a script."""
import asyncio
import json
import os
import sys
import time

from aiohttp import ClientError

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HIER, "..", "router"))
from skirnir_router import tunnel   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


class FakeWS:
    """Agent side: records sent frames. answer: None = silent, "ok" = RESP 200, "err" = ERR frame.
    hold_send: REQ send hangs until the event is set (WebSocket backpressure)."""

    def __init__(self, answer=None, hold_send=None):
        self.answer, self.hold_send, self.frames, self.tun = answer, hold_send, [], None

    async def send_bytes(self, frame):
        if frame[0] == tunnel.TUN_REQ and self.hold_send is not None:
            await self.hold_send.wait()
        self.frames.append(frame)
        if frame[0] != tunnel.TUN_REQ or self.answer is None:
            return
        sid = int.from_bytes(frame[1:5], "big")
        loop = asyncio.get_running_loop()
        if self.answer == "ok":
            loop.call_soon(self.tun.dispatch, tunnel.tun_frame(tunnel.TUN_RESP, sid, json.dumps({"status": 200, "headers": {}}).encode()))
            loop.call_soon(self.tun.dispatch, tunnel.tun_frame(tunnel.TUN_DATA, sid, b"{}"))
            loop.call_soon(self.tun.dispatch, tunnel.tun_frame(tunnel.TUN_END, sid))
        else:
            loop.call_soon(self.tun.dispatch, tunnel.tun_frame(tunnel.TUN_ERR, sid, b"upstream weg"))

    def cancels(self):
        return [int.from_bytes(f[1:5], "big") for f in self.frames if f[0] == tunnel.TUN_CANCEL]

    def requests(self):
        return [int.from_bytes(f[1:5], "big") for f in self.frames if f[0] == tunnel.TUN_REQ]


def make(**kw):
    ws = FakeWS(**kw)
    tun = tunnel.Tunnel(None, ws, "127.0.0.1")
    ws.tun = tun
    return tun, ws


async def settle():
    for _ in range(5):   # let spawned CANCEL send and call_soon frames run
        await asyncio.sleep(0)


async def scenarios():
    # 1. Caller is aborted while the agent is still computing (client disconnects during prefill)
    tun, ws = make()
    task = asyncio.create_task(tun.open("POST", "/api/chat", b"{}", None))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
        cancelled = False
    except asyncio.CancelledError:   # the abort must reach the caller
        cancelled = True
    await settle()
    check("Abbruch kommt beim Aufrufer an", cancelled)
    check("Abbruch: Stream entfernt", tun.streams == {}, str(tun.streams))
    check("Abbruch: genau ein CANCEL fuer den Stream", ws.cancels() == ws.requests() == [1], f"{ws.cancels()} {ws.requests()}")

    # 2. External timeout shorter than the tunnel deadline (like proxy._connect with `await call`)
    tun, ws = make()
    try:
        await asyncio.wait_for(tun.request("POST", "/api/chat", json={}), 0.1)
        timed_out = False
    except TimeoutError:   # asyncio.wait_for reports the expiry as TimeoutError
        timed_out = True
    await settle()
    check("aeussere Zeitgrenze: TimeoutError beim Aufrufer", timed_out)
    check("aeussere Zeitgrenze: Stream entfernt, CANCEL gesendet", tun.streams == {} and ws.cancels() == [1], f"{tun.streams} {ws.cancels()}")

    # 3. Tunnel's own deadline: cleaned up as before, no double CANCEL
    tun, ws = make()
    try:
        await tun.open("POST", "/api/chat", b"{}", time.time() + 0.05)
        own_timeout = False
    except TimeoutError:
        own_timeout = True
    await settle()
    check("eigene Frist: TimeoutError, Stream entfernt, genau ein CANCEL",
          own_timeout and tun.streams == {} and ws.cancels() == [1], f"{tun.streams} {ws.cancels()}")

    # 4. Abort while REQ send is still hanging (WebSocket backpressure)
    gate = asyncio.Event()
    tun, ws = make(hold_send=gate)
    task = asyncio.create_task(tun.open("POST", "/api/chat", b"{}", None))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    gate.set()
    await settle()
    check("Abbruch beim Versand: Stream entfernt", tun.streams == {}, str(tun.streams))
    check("Abbruch beim Versand: CANCEL nachgeschickt (Agent ignoriert unbekannte Streams)", ws.cancels() == [1], str(ws.cancels()))

    # 5. Normal case unchanged: response header, body, stream removed after reading, no CANCEL
    tun, ws = make(answer="ok")
    async with tun.request("POST", "/api/chat", json={}) as r:
        status, body = r.status, await r.read()
    await settle()
    check("Normalfall: 200 und Body", status == 200 and body == b"{}", f"{status} {body}")
    check("Normalfall: Stream weg, kein CANCEL", tun.streams == {} and ws.cancels() == [], f"{tun.streams} {ws.cancels()}")

    # 6. Agent ERR frame: ClientError as before, stream removed, no CANCEL
    tun, ws = make(answer="err")
    try:
        await tun.open("POST", "/api/chat", b"{}", None)
        err = False
    except ClientError:
        err = True
    await settle()
    check("ERR-Rahmen: ClientError, Stream weg, kein CANCEL", err and tun.streams == {} and ws.cancels() == [], f"{tun.streams} {ws.cancels()}")


asyncio.run(scenarios())
print(f"\n{len(FAILS)} failures: {FAILS}")
sys.exit(1 if FAILS else 0)
