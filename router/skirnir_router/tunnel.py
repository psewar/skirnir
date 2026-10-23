"""Agent -> Router: WebSocket tunnel with binary frames (REQ/RESP/DATA/END/ERR/CANCEL/HB/HBACK/OLLAMA)."""

import asyncio
import json
import time

from aiohttp import ClientError

from . import poll, state
from .common import log


# The agent holds an outgoing WebSocket connection; the router sends its Ollama calls as streams through it.
# Rahmen: typ(1) | stream_id(4, big endian) | payload. Gegenstueck: agent/tunnel.go, test/fake_agent.py.
TUN_REQ, TUN_RESP, TUN_DATA, TUN_END, TUN_ERR, TUN_CANCEL, TUN_HB, TUN_HBACK = 1, 2, 3, 4, 5, 6, 7, 8
TUN_OLLAMA = 9   # Agent -> Router: Ollama-Zustand (installiert, geladen, erreichbar), Agent >= 0.14.0


def tun_frame(t, sid, payload=b""):
    return bytes([t]) + sid.to_bytes(4, "big") + payload


def response_head(payload):
    """(status, headers) from the agent's RESP frame, None if unreadable."""
    try:
        h = json.loads(payload)
    except ValueError:  # exception-ok: untrusted JSON from the agent
        return None
    if not isinstance(h, dict) or isinstance(h.get("status"), bool) or not isinstance(h.get("status"), int):
        return None
    headers = h.get("headers") or {}
    return (h["status"], headers) if isinstance(headers, dict) else None


class TunnelResponse:
    """Response of a tunnel stream. Provides what the router uses from aiohttp.ClientResponse:
    status, headers, read()/text()/json(), `async for line in resp.content`, `resp.content.iter_any()` /
    `iter_chunked(n)`, async-with. The chunk iterators came with OpenAI-compatible nodes (cloud._sse reads with
    iter_any); managed.lines and proxy._complete_unary read with iter_chunked."""

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
            # exception-ok: emulates aiohttp's ClientResponse; callers handle the timeout at their network boundary
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
        # exception-ok: emulates aiohttp's ClientResponse; a broken stream is a ClientError at the caller's network boundary
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

    async def iter_any(self):
        """Data as it arrives (aiohttp StreamReader.iter_any): what line iteration has buffered first, then each frame."""
        if self.buf:
            data, self.buf = self.buf, b""
            yield data
        while (c := await self._next()) is not None:
            if c:
                yield c

    async def iter_chunked(self, n):
        """Data in pieces of at most n bytes (aiohttp StreamReader.iter_chunked)."""
        async for data in self.iter_any():
            for i in range(0, len(data), n):
                yield data[i:i + n]

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
                raise StopAsyncIteration  # exception-ok: async iterator protocol, end of the stream
            self.buf += c

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if not self.done:
            await self.tunnel.cancel(self.sid)
        self.tunnel.streams.pop(self.sid, None)


class TunnelRequest:
    """Return value of Tunnel.request(): usable via `async with` or `await` like SESSION.request()."""

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
        self.node, self.ws, self.remote = node, ws, remote   # node None = waiting for approval
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
        """Fire-and-forget (HBACK): if the tunnel is closing (router stop, agent gone), this is not an error - without this
        wrapper, every deploy logged 'Task exception was never retrieved' with a traceback (2026-09-25)."""
        try:
            await self.send(frame)
        except (ConnectionError, RuntimeError, ClientError) as e:  # exception-ok: network boundary, the websocket is closing
            log.debug("tunnel %s: answer dropped, connection closing (%s)", self.name(), e)

    async def open(self, method, path, body, deadline):
        """Open the stream and wait for the response header. If the caller is aborted earlier (client disconnects during
        Prefills, outer timeout), open cleans up the stream and sends CANCEL: otherwise the entry stayed in `streams`
        until the tunnel ended, and the agent finished the Ollama request for no one."""
        sid = self.next_id
        self.next_id = self.next_id % 0xFFFFFFFF + 1
        resp = TunnelResponse(self, sid, deadline)
        self.streams[sid] = resp
        self.requests += 1
        try:
            return await self._open(sid, resp, method, path, body, deadline)
        except asyncio.CancelledError:  # exception-ok: cancellation of the waiting caller; clean up the stream, then pass it on
            if self.streams.pop(sid, None) is not None:
                state.spawn(self.cancel(sid))   # do not wait here: the abort should continue immediately
            raise  # exception-ok: cancellation must propagate to the caller

    async def _open(self, sid, resp, method, path, body, deadline):
        head = {"method": method, "path": path, "headers": ({"Content-Type": "application/json"} if body else {})}
        try:
            await self.send(tun_frame(TUN_REQ, sid, json.dumps(head).encode() + b"\n" + body))
        except Exception as e:  # noqa: BLE001  # exception-ok: network boundary of the websocket send
            self.streams.pop(sid, None)
            # exception-ok: emulates aiohttp; the caller handles a ClientError at its network boundary
            raise ClientError(f"tunnel {self.name()}: send failed: {e}") from e
        wait = (deadline - time.time()) if deadline else 600   # Response header arrives from Ollama only after model load
        try:
            t, payload = await asyncio.wait_for(resp.q.get(), max(0.001, wait))
        except asyncio.TimeoutError:  # exception-ok: deadline of the emulated request; clean up the stream, then pass it on
            self.streams.pop(sid, None)
            await self.cancel(sid)
            raise  # exception-ok: the caller handles the timeout like aiohttp's at its network boundary
        head = response_head(payload) if t == TUN_RESP else None
        if head is not None:
            resp.status, resp.headers = head
            return resp
        self.streams.pop(sid, None)
        why = payload.decode(errors="replace") if t == TUN_ERR else "unlesbarer Antwortkopf" if t == TUN_RESP else "unerwarteter Rahmen"
        # exception-ok: emulates aiohttp; the caller handles a ClientError at its network boundary
        raise ClientError(f"tunnel {self.name()}: {why}")

    async def cancel(self, sid):
        try:
            await self.send(tun_frame(TUN_CANCEL, sid))
        except Exception as e:  # noqa: BLE001  # exception-ok: network boundary; a lost CANCEL ends with the tunnel anyway
            log.debug("tunnel %s: CANCEL dropped (%s)", self.name(), e)

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
                # the heartbeat is untrusted agent input; a malformed field must not tear down the tunnel
                except Exception as e:  # noqa: BLE001  # exception-ok: untrusted heartbeat, the reason goes back in the ack
                    ack = {"state": "", "error": str(e)}
            else:
                ack = {"state": "pending", "busy_reason": ""}
            state.spawn(self.send_quiet(tun_frame(TUN_HBACK, 0, json.dumps(ack).encode())))
            return
        if t == TUN_OLLAMA:
            if self.approved and self.node is not None:
                try:
                    msg = json.loads(payload)
                except ValueError:  # exception-ok: untrusted JSON from the agent; whether it parses shows only the parser
                    log.warning("node %s: Ollama state from the agent is not JSON", self.node.name)
                    return
                poll.apply_ollama_push(self.node, msg, time.time())
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
