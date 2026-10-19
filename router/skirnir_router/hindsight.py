"""Bounded official Hindsight REST adapter; no redirects or ambient credentials.

Every call returns its result or a Problem (outcome.py) whose `kind` is one of KINDS.
Network faults are caught at the HTTP boundary in `_exchange` and nowhere else.
"""
import asyncio
import os
import re
import time
from datetime import datetime, timezone
from uuid import UUID

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TCPConnector

from . import metrics
from .outcome import Problem, denied, failed, invalid, unavailable
from .session_api import parse_json_value

KINDS = ("unavailable", "upstream_auth", "upstream_rejected", "protocol", "timeout", "transport", "remote_pending")
RETRY_STATUSES = (429, 500, 502, 503, 504)
BANK_TEXT = re.compile(r"skirnir-[0-9a-f]{64}")
# Host names, IPv4 or the IPv6 loopback literal; an optional path prefix of plain URL characters.
URL_TEXT = re.compile(r"(?P<scheme>https?)://(?P<host>[A-Za-z0-9.-]+|\[::1\])(?::(?P<port>[0-9]{1,5}))?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?")
LOOPBACK_V4 = re.compile(r"127(?:\.[0-9]{1,3}){3}")
POLICY_CHANGED = denied("memory policy changed before dispatch")


def unavailable_as(kind):
    return unavailable("memory backend unavailable", kind if kind in KINDS else "unavailable")


def is_loopback(host):
    return host in ("localhost", "[::1]") or bool(LOOPBACK_V4.fullmatch(host))


def endpoint_from_env(cfg):
    """Validated Hindsight base URL from the environment, or a Problem. The whole URL is matched against
    URL_TEXT (scheme, host, port, path; no credentials, query or fragment by construction)."""
    value = os.environ.get(cfg.endpoint_env, "").rstrip("/")
    match = URL_TEXT.fullmatch(value)
    host = match["host"].lower() if match else ""
    port_ok = match is not None and (match["port"] is None or 1 <= int(match["port"]) <= 65535)
    valid_scheme = match is not None and (match["scheme"] == "https" or (cfg.allow_http_loopback and is_loopback(host)))
    if not port_ok or not valid_scheme:
        return invalid("Hindsight endpoint must be HTTPS without credentials/query/fragment; HTTP is opt-in for loopback only")
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

    async def _exchange(self, method, url, payload, authorize, read_statuses):
        """One bounded HTTP exchange: (status, parsed JSON object or None), or a Problem. `authorize` is checked
        inside the concurrency slot, right before sending; False means the policy changed while waiting."""
        try:
            async with asyncio.timeout(self.cfg.timeout_s), self.slots:
                if authorize is not None and not authorize():
                    return POLICY_CHANGED
                async with self.http.request(method, url, json=payload, allow_redirects=False) as response:
                    if response.status not in read_statuses:
                        return response.status, None
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        data.extend(chunk)
                        if len(data) > self.cfg.max_response_bytes:
                            return unavailable_as("protocol")
                    status = response.status
        except TimeoutError:   # exception-ok: network faults cannot be checked beforehand; this is their only catch
            return unavailable_as("timeout")
        except (ClientError, OSError):   # exception-ok: same network boundary (connection, TLS, reset)
            return unavailable_as("transport")
        value = parse_json_value(bytes(data))
        if failed(value) or not isinstance(value, dict):
            return unavailable_as("protocol")
        return status, value

    async def post(self, bank, suffix, payload, *, missing_empty=False, authorize=None):
        started, outcome = time.monotonic(), "error"
        result = unavailable_as("unavailable")
        if BANK_TEXT.fullmatch(bank):
            for attempt in range(self.cfg.retries + 1):
                reply = await self._exchange("POST", self.endpoint + "/v1/default/banks/" + bank + "/memories" + suffix,
                                             payload, authorize, (200, 201, 202))
                if isinstance(reply, Problem):
                    result = reply
                    # Only connection faults and timeouts are retried; never a policy change or a protocol violation.
                    if reply.kind not in ("timeout", "transport") or attempt == self.cfg.retries:
                        break
                    await asyncio.sleep(0.05)
                    continue
                status, value = reply
                if status == 404 and missing_empty:
                    outcome, result = "empty", {"results": []}
                    break
                if status in RETRY_STATUSES:
                    result = unavailable_as("unavailable")
                    if attempt == self.cfg.retries:
                        break
                    await asyncio.sleep(0.05)
                    continue
                if value is None:
                    result = unavailable_as("upstream_auth" if status in (401, 403) else "upstream_rejected")
                    break
                outcome, result = "success", value
                break
        self.healthy = outcome in ("success", "empty")
        operation = "recall" if suffix else "retain"
        metrics._counter("skirnir_memory_http_total", {"operation": operation, "outcome": outcome})
        metrics._hist("skirnir_memory_http_seconds", {"operation": operation}, metrics.DURATION_BUCKETS, time.monotonic() - started)
        return result

    async def recall(self, bank, query, *, types=None, authorize=None):
        # Entities are on by default upstream but never passed on by Skirnir (measured: ~14 % of a 85 KB answer).
        # Hindsight documents null as "skip the entity JOIN". The fact count, set by recall_max_tokens, drives the size.
        payload = {"query": query, "budget": "low", "max_tokens": self.cfg.recall_max_tokens, "include": {"entities": None}}
        if types is not None:
            payload["types"] = types
        return await self.post(bank, "/recall", payload, missing_empty=True, authorize=authorize)

    async def retain(self, bank, content, operation_id, *, document_id=None, confirmed, provenance=None, shared_observations=False, authorize=None):
        """None once Hindsight acknowledged the operation, or a Problem. The document id defaults to the operation id;
        a resubmission sends a new operation id for the same document."""
        document_id = document_id or operation_id
        origin = {"kind": "confirmed_information" if confirmed else "assistant_derivation",
                  "origin_client": "unspecified", "observed_at": datetime.now(timezone.utc).isoformat(), "version": document_id}
        origin.update(provenance or {})
        result = await self.post(bank, "", {"async": True, "operation_id": operation_id, "items": [{
            "content": content, "document_id": document_id,
            **({"observation_scopes": "shared"} if shared_observations else {}),
            "context": "Client-confirmed knowledge" if confirmed else "Unverified assistant response; not confirmed facts",
            "metadata": {**origin, "source": "client" if confirmed else "assistant", "confirmed": str(confirmed).lower()}}]}, authorize=authorize)
        if failed(result):
            return result
        return None if result.get("operation_id") == operation_id else unavailable_as("protocol")

    async def control(self, bank, path, method="GET", authorize=None):
        self.healthy = False
        if not BANK_TEXT.fullmatch(bank):
            return unavailable_as("unavailable")
        reply = await self._exchange(method, self.endpoint + "/v1/default/banks/" + bank + path, None, authorize, (200,))
        if isinstance(reply, Problem):
            return reply
        status, value = reply
        if status == 404:
            self.healthy = True
            return {"status": "not_found"} if method == "GET" else {"deleted": True}
        if value is None:
            return unavailable_as("upstream_auth" if status in (401, 403) else "upstream_rejected")
        self.healthy = True
        return value

    async def operation(self, bank, operation_id, *, authorize=None):
        return await self.control(bank, "/operations/" + str(UUID(operation_id)), authorize=authorize)

    async def delete_document(self, bank, document_id, *, authorize=None):
        return await self.control(bank, "/documents/" + str(UUID(document_id)), "DELETE", authorize=authorize)
