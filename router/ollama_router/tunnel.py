"""Agent -> Router: WebSocket-Tunnel mit Binaerrahmen (REQ/RESP/DATA/END/ERR/CANCEL/HB/HBACK)."""

import asyncio
import json
import time

from aiohttp import ClientError

from . import poll
from .common import log


# Der Agent haelt eine ausgehende WebSocket-Verbindung; der Router schickt seine Ollama-Aufrufe als Streams hindurch.
# Rahmen: typ(1) | stream_id(4, big endian) | payload. Gegenstueck: agent-go/tunnel.go, test/fake_agent.py.
TUN_REQ, TUN_RESP, TUN_DATA, TUN_END, TUN_ERR, TUN_CANCEL, TUN_HB, TUN_HBACK = 1, 2, 3, 4, 5, 6, 7, 8


def tun_frame(t, sid, payload=b""):
    return bytes([t]) + sid.to_bytes(4, "big") + payload


class TunnelResponse:
    """Antwort eines Tunnel-Streams. Bietet, was der Router von aiohttp.ClientResponse nutzt:
    status, headers, read()/text()/json(), `async for line in resp.content`, async-with."""

    def __init__(self, tunnel, sid, deadline):
        self.tunnel, self.sid, self.deadline = tunnel, sid, deadline
        self.status, self.headers = None, {}
        self.q = asyncio.Queue()
        self.buf = b""
        self.done = False
        self.content = self

    def _remaining(self):
        if self.deadline is None:
            return None
        r = self.deadline - time.time()
        if r <= 0:
            raise asyncio.TimeoutError()
        return r

    async def _next(self):
        if self.done:
            return None
        t, payload = await asyncio.wait_for(self.q.get(), self._remaining())
        if t == TUN_DATA:
            return payload
        self.done = True
        if t == TUN_END:
            return None
        raise ClientError(f"tunnel {self.tunnel.name()}: {payload.decode(errors='replace')}")

    async def read(self):
        chunks, self.buf = [self.buf], b""
        while True:
            c = await self._next()
            if c is None:
                return b"".join(chunks)
            chunks.append(c)

    async def text(self):
        return (await self.read()).decode("utf-8", errors="replace")

    async def json(self, content_type=None):
        return json.loads(await self.read() or b"null")

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[:i + 1], self.buf[i + 1:]
                return line
            c = await self._next()
            if c is None:
                if self.buf:
                    line, self.buf = self.buf, b""
                    return line
                raise StopAsyncIteration
            self.buf += c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if not self.done:
            await self.tunnel.cancel(self.sid)
        self.tunnel.streams.pop(self.sid, None)

    def release(self):
        pass


class TunnelRequest:
    """Rueckgabe von Tunnel.request(): wie SESSION.request() per `async with` oder `await` nutzbar."""

    def __init__(self, tunnel, method, path, json_body=None, timeout=None):
        self.tunnel, self.method, self.path = tunnel, method.upper(), path
        self.body = json.dumps(json_body).encode() if json_body is not None else b""
        total = getattr(timeout, "total", None) if timeout is not None else None
        self.deadline = time.time() + total if total else None
        self.resp = None

    async def __aenter__(self):
        self.resp = await self.tunnel.open(self.method, self.path, self.body, self.deadline)
        return self.resp

    async def __aexit__(self, *exc):
        if self.resp is not None:
            await self.resp.__aexit__(*exc)

    def __await__(self):
        return self.__aenter__().__await__()


class Tunnel:
    def __init__(self, node, ws, remote):
        self.node, self.ws, self.remote = node, ws, remote   # node None = wartet auf Freigabe
        self.fp = None
        self.approved = node is not None
        self.streams = {}
        self.next_id = 1
        self.lock = asyncio.Lock()
        self.since = time.time()
        self.requests = 0

    def request(self, method, path, **kw):
        return TunnelRequest(self, method, path, json_body=kw.get("json"), timeout=kw.get("timeout"))

    async def send(self, frame):
        async with self.lock:
            await self.ws.send_bytes(frame)

    async def send_quiet(self, frame):
        """Fire-and-forget (HBACK): schliesst der Tunnel gerade (Router-Stopp, Agent weg), ist das kein Fehler - ohne diese
        Huelle stand bei jedem Deploy 'Task exception was never retrieved' mit Traceback im Journal (2026-09-25)."""
        try:
            await self.send(frame)
        except (ConnectionError, RuntimeError, ClientError) as e:
            log.debug("tunnel %s: Antwort verworfen, Verbindung schliesst (%s)", self.name(), e)

    async def open(self, method, path, body, deadline):
        sid = self.next_id
        self.next_id = self.next_id % 0xFFFFFFFF + 1
        resp = TunnelResponse(self, sid, deadline)
        self.streams[sid] = resp
        self.requests += 1
        head = {"method": method, "path": path, "headers": ({"Content-Type": "application/json"} if body else {})}
        try:
            await self.send(tun_frame(TUN_REQ, sid, json.dumps(head).encode() + b"\n" + body))
        except Exception as e:  # noqa: BLE001
            self.streams.pop(sid, None)
            raise ClientError(f"tunnel {self.name()}: senden fehlgeschlagen: {e}")
        wait = (deadline - time.time()) if deadline else 600   # Antwortkopf kommt bei Ollama erst nach dem Modell-Load
        try:
            t, payload = await asyncio.wait_for(resp.q.get(), max(0.001, wait))
        except asyncio.TimeoutError:
            self.streams.pop(sid, None)
            await self.cancel(sid)
            raise
        if t == TUN_RESP:
            h = json.loads(payload)
            resp.status, resp.headers = int(h["status"]), h.get("headers") or {}
            return resp
        self.streams.pop(sid, None)
        raise ClientError(f"tunnel {self.name()}: {payload.decode(errors='replace') if t == TUN_ERR else 'unerwarteter Rahmen'}")

    async def cancel(self, sid):
        try:
            await self.send(tun_frame(TUN_CANCEL, sid))
        except Exception:  # noqa: BLE001
            pass

    def name(self):
        return self.node.name if self.node is not None else f"pending-{(self.fp or '')[:12]}"

    def dispatch(self, data):
        if len(data) < 5:
            return
        t, sid, payload = data[0], int.from_bytes(data[1:5], "big"), data[5:]
        if t == TUN_HB:
            if self.approved and self.node is not None:
                try:
                    poll.apply_heartbeat(self.node, json.loads(payload), time.time())
                    ack = poll.hb_ack(self.node)
                except Exception as e:  # noqa: BLE001
                    ack = {"state": "", "error": str(e)}
            else:
                ack = {"state": "pending", "busy_reason": ""}
            asyncio.create_task(self.send_quiet(tun_frame(TUN_HBACK, 0, json.dumps(ack).encode())))
            return
        r = self.streams.get(sid)
        if r is None:
            return
        r.q.put_nowait((t, payload))
        if t in (TUN_END, TUN_ERR):
            self.streams.pop(sid, None)

    def close_all(self, reason):
        for r in list(self.streams.values()):
            r.q.put_nowait((TUN_ERR, reason.encode()))
        self.streams.clear()
