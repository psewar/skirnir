"""Persistent retain submission and bounded multi-router retry worker."""
import asyncio
import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from . import metrics
from .hindsight import MemoryUnavailable
from .session_types import SessionUnavailable, SessionConflict


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def submit(service, scope, event_id, content, confirmed, provenance=None):
    operation = service.validate_retain(scope, event_id, content)
    from .memory import rejected_content, MemoryDenied
    from .memory_gateway import provenance as make_provenance
    origin = provenance or make_provenance({}, scope.principal, confirmed=confirmed)
    if rejected_content(json.dumps(origin), service.cfg):
        raise MemoryDenied("sensitive provenance")
    payload = {"content": content, "confirmed": confirmed, "provenance": origin}
    await service.outbox.outbox_put(UUID(operation), {**asdict(scope), "__bank": service.bank(scope)}, payload, digest(payload), service.cfg.outbox_capacity)
    service.wakeup.set()
    return operation


async def step(service):
    from .memory import Scope, MemoryDenied, count, rejected_content
    row = await service.outbox.outbox_claim(service.cfg.outbox_lease_s, service.cfg.outbox_attempts)
    if row is None:
        return False
    scope = Scope(**{k: v for k, v in row["scope"].items() if k in Scope.__dataclass_fields__})
    operation, lease = UUID(row["operation_id"]), UUID(row["lease"])
    status, delay = "revoked", 0
    try:
        if service.bank(scope) != row["scope"].get("__bank"):
            raise MemoryDenied("namespace binding changed")
        payload = row["payload"]
        if row.get("delete_requested"):
            if service.current(scope) and scope.delete:
                upstream = await service.client.operation(service.bank(scope), str(operation), authorize=lambda: service.guard(scope))
                if upstream.get("status") != "completed":
                    raise MemoryUnavailable
                await service.client.delete_document(service.bank(scope), str(operation), authorize=lambda: service.guard(scope))
                status = "deleted"
        elif service.cfg.retention_enabled and service.current(scope, write=True) and not rejected_content(payload["content"], service.cfg):
            await service.client.retain(service.bank(scope), payload["content"], str(operation),
                                        confirmed=payload["confirmed"], provenance=payload.get("provenance"), authorize=lambda: service.guard(scope, write=True))
            status = "accepted"
    except MemoryDenied:
        status = "revoked"
    except MemoryUnavailable:
        status = "dead" if row["attempts"] >= service.cfg.outbox_attempts else ("delete_pending" if row.get("delete_requested") else "pending")
        delay = min(3600, service.cfg.outbox_backoff_s * 2 ** min(row["attempts"] - 1, 12))
    await service.outbox.outbox_finish(operation, lease, status, delay)
    count("outbox", status)
    return True


async def run(service):
    while True:
        try:
            if service.active() and await step(service):
                continue
        except (SessionUnavailable, SessionConflict) as exc:
            outcome = "lease_lost" if isinstance(exc, SessionConflict) else "database_unavailable"
            metrics._counter("skirnir_memory_events_total", {"operation": "outbox", "outcome": outcome})
        service.wakeup.clear()
        try:
            await asyncio.wait_for(service.wakeup.wait(), timeout=1)
        except TimeoutError:
            pass
