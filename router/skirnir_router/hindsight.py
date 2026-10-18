"""Bounded official Hindsight REST adapter; no redirects or ambient credentials."""
import asyncio
import ipaddress
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector

from . import metrics


class MemoryUnavailable(Exception):
    def __init__(self, message=None, *, kind="unavailable"):
        super().__init__(message or "memory backend unavailable")
        self.kind = kind if kind in ("unavailable", "upstream_auth", "upstream_rejected", "protocol", "timeout", "transport", "remote_pending") else "unavailable"



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
        self.healthy = None

    async def close(self):
        await self.http.close()

    async def post(self, bank, suffix, payload, *, missing_empty=False, authorize=None):
        started, outcome = time.monotonic(), "error"
        try:
            if not bank.startswith("skirnir-") or len(bank) != 72 or any(c not in "0123456789abcdef" for c in bank[8:]):
                raise MemoryUnavailable
            for attempt in range(self.cfg.retries + 1):
                try:
                    async with asyncio.timeout(self.cfg.timeout_s), self.slots:
                        if authorize is not None:
                            authorize()
                        async with self.http.post(self.endpoint + "/v1/default/banks/" + bank + "/memories" + suffix,
                                                  json=payload, allow_redirects=False) as response:
                            if response.status == 404 and missing_empty:
                                outcome = "empty"
                                return {"results": []}
                            if response.status in (429, 500, 502, 503, 504):
                                raise _Retryable
                            if response.status not in (200, 201, 202):
                                raise MemoryUnavailable(kind="upstream_auth" if response.status in (401, 403) else "upstream_rejected")
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
                except (_Retryable, ClientError, OSError, TimeoutError) as exc:
                    if attempt == self.cfg.retries:
                        raise MemoryUnavailable(kind="timeout" if isinstance(exc, TimeoutError) else "transport" if isinstance(exc, (ClientError, OSError)) else "unavailable") from None
                    await asyncio.sleep(0.05)
            raise MemoryUnavailable
        except (ValueError, RecursionError):
            raise MemoryUnavailable(kind="protocol") from None
        finally:
            self.healthy = outcome in ("success", "empty")
            operation = "recall" if suffix else "retain"
            metrics._counter("skirnir_memory_http_total", {"operation": operation, "outcome": outcome})
            metrics._hist("skirnir_memory_http_seconds", {"operation": operation}, metrics.DURATION_BUCKETS, time.monotonic() - started)

    async def recall(self, bank, query, *, types=None, authorize=None):
        payload = {"query": query, "budget": "low", "max_tokens": self.cfg.recall_max_tokens}
        if types is not None:
            payload["types"] = types
        return await self.post(bank, "/recall", payload, missing_empty=True, authorize=authorize)

    async def retain(self, bank, content, operation_id, *, confirmed, provenance=None, shared_observations=False, authorize=None):
        origin = {"kind": "confirmed_information" if confirmed else "assistant_derivation",
                  "origin_client": "unspecified", "observed_at": datetime.now(timezone.utc).isoformat(), "version": operation_id}
        origin.update(provenance or {})
        result = await self.post(bank, "", {"async": True, "operation_id": operation_id, "items": [{
            "content": content, "document_id": operation_id,
            **({"observation_scopes": "shared"} if shared_observations else {}),
            "context": "Client-confirmed knowledge" if confirmed else "Unverified assistant response; not confirmed facts",
            "metadata": {**origin, "source": "client" if confirmed else "assistant", "confirmed": str(confirmed).lower()}}]}, authorize=authorize)
        if result.get("operation_id") != operation_id:
            raise MemoryUnavailable(kind="protocol")


    async def control(self, bank, path, method="GET", authorize=None):
        self.healthy = False
        if not bank.startswith("skirnir-") or len(bank) != 72 or any(c not in "0123456789abcdef" for c in bank[8:]):
            raise MemoryUnavailable
        try:
            async with asyncio.timeout(self.cfg.timeout_s), self.slots:
                if authorize is not None:
                    authorize()
                async with self.http.request(method, self.endpoint + "/v1/default/banks/" + bank + path, allow_redirects=False) as response:
                    if response.status == 404:
                        self.healthy = True
                        return {"status": "not_found"} if method == "GET" else {"deleted": True}
                    if response.status != 200:
                        raise MemoryUnavailable(kind="upstream_auth" if response.status in (401, 403) else "upstream_rejected")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        data.extend(chunk)
                        if len(data) > self.cfg.max_response_bytes:
                            raise MemoryUnavailable
                    value = json.loads(data)
                    if not isinstance(value, dict):
                        raise MemoryUnavailable
                    self.healthy = True
                    return value
        except (ClientError, OSError, TimeoutError, ValueError, RecursionError) as exc:
            raise MemoryUnavailable(kind="timeout" if isinstance(exc, TimeoutError) else "protocol" if isinstance(exc, (ValueError, RecursionError)) else "transport") from None

    async def operation(self, bank, operation_id, *, authorize=None):
        from uuid import UUID
        ident = str(UUID(operation_id))
        return await self.control(bank, "/operations/" + ident, authorize=authorize)

    async def delete_document(self, bank, document_id, *, authorize=None):
        from uuid import UUID
        ident = str(UUID(document_id))
        return await self.control(bank, "/documents/" + ident, "DELETE", authorize=authorize)
