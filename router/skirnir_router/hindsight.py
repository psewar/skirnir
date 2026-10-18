"""Bounded official Hindsight REST adapter; no redirects or ambient credentials."""
import asyncio
import ipaddress
import json
import os
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector

from . import metrics


class MemoryUnavailable(Exception):
    pass


class _Retryable(Exception):
    pass


def endpoint_from_env(cfg):
    value = os.environ.get(cfg.endpoint_env, "").rstrip("/")
    try:
        url = urlsplit(value)
        _ = url.port
        loopback = url.hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(url.hostname or "").is_loopback
        except ValueError:
            pass
        valid_scheme = url.scheme == "https" or (url.scheme == "http" and cfg.allow_http_loopback and loopback)
        if not valid_scheme or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError
    except ValueError:
        raise ValueError("Hindsight endpoint must be HTTPS without credentials/query/fragment; HTTP is opt-in for loopback only") from None
    return value


class HindsightClient:
    def __init__(self, endpoint, token, cfg):
        self.endpoint, self.cfg = endpoint, cfg
        headers = {"Authorization": "Bearer " + token} if token else {}
        self.http = ClientSession(timeout=ClientTimeout(total=cfg.timeout_s), headers=headers, trust_env=False,
                                  cookie_jar=DummyCookieJar(), connector=TCPConnector(limit=cfg.max_inflight))
        self.slots = asyncio.Semaphore(cfg.max_inflight)

    async def close(self):
        await self.http.close()

    async def post(self, bank, suffix, payload, *, missing_empty=False):
        started, outcome = time.monotonic(), "error"
        try:
            if not bank.startswith("skirnir-") or len(bank) != 72 or any(c not in "0123456789abcdef" for c in bank[8:]):
                raise MemoryUnavailable
            for attempt in range(self.cfg.retries + 1):
                try:
                    async with asyncio.timeout(self.cfg.timeout_s), self.slots:
                        async with self.http.post(self.endpoint + "/v1/default/banks/" + bank + "/memories" + suffix,
                                                  json=payload, allow_redirects=False) as response:
                            if response.status == 404 and missing_empty:
                                outcome = "empty"
                                return {"results": []}
                            if response.status in (429, 500, 502, 503, 504):
                                raise _Retryable
                            if response.status not in (200, 201, 202):
                                raise MemoryUnavailable
                            data = bytearray()
                            async for chunk in response.content.iter_chunked(8192):
                                data.extend(chunk)
                                if len(data) > self.cfg.max_response_bytes:
                                    raise MemoryUnavailable
                            result = json.loads(data)
                            if not isinstance(result, dict):
                                raise MemoryUnavailable
                            outcome = "success"
                            return result
                except (_Retryable, ClientError, OSError, TimeoutError):
                    if attempt == self.cfg.retries:
                        raise MemoryUnavailable from None
                    await asyncio.sleep(0.05)
            raise MemoryUnavailable
        except (ValueError, RecursionError):
            raise MemoryUnavailable from None
        finally:
            operation = "recall" if suffix else "retain"
            metrics._counter("skirnir_memory_http_total", {"operation": operation, "outcome": outcome})
            metrics._hist("skirnir_memory_http_seconds", {"operation": operation}, metrics.DURATION_BUCKETS, time.monotonic() - started)

    async def recall(self, bank, query):
        return await self.post(bank, "/recall", {"query": query, "budget": "low", "max_tokens": self.cfg.recall_max_tokens}, missing_empty=True)

    async def retain(self, bank, content, operation_id, *, confirmed):
        result = await self.post(bank, "", {"async": True, "operation_id": operation_id, "items": [{
            "content": content, "document_id": operation_id,
            "context": "Client-confirmed knowledge" if confirmed else "Unverified assistant response; not confirmed facts",
            "metadata": {"source": "client" if confirmed else "assistant", "confirmed": str(confirmed).lower()}}]})
        if result.get("operation_id") != operation_id:
            raise MemoryUnavailable("Hindsight deployment did not acknowledge the idempotent async operation")
