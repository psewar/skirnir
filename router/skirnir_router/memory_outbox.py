"""Persistent retain submission and bounded multi-router retry worker.

Functions return their result or a Problem (outcome.py); the worker records each
problem as a fixed state and diagnostic category on the outbox row.
"""
import asyncio
import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from . import metrics
from .outcome import denied, failed


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def submit(service, scope, event_id, content, confirmed, provenance=None, shared_observations=False):
    """Operation UUID admitted durably, or a Problem."""
    operation = service.validate_retain(scope, event_id, content)
    if failed(operation):
        return operation
    from .memory import rejected_content
    from .memory_gateway import provenance as make_provenance
    origin = provenance or make_provenance({}, scope.principal, confirmed=confirmed)
    if failed(origin):
        return origin
    if rejected_content(json.dumps(origin), service.cfg):
        return denied("sensitive provenance")
    payload = {"content": content, "confirmed": confirmed, "provenance": origin}
    if shared_observations:
        payload["shared_observations"] = True
    admitted = await service.outbox.outbox_put(UUID(operation), {**asdict(scope), "__bank": service.bank(scope)}, payload, digest(payload),
                                               service.cfg.outbox_capacity, **({"owner_capacity": service.cfg.outbox_client_capacity} if service.cfg.outbox_client_capacity else {}))
    if failed(admitted):
        return admitted
    service.wakeup.set()
    return operation


async def deliver(service, scope, operation, row):
    """Hand one claimed job to Hindsight. (status, error category) for the outbox row."""
    if service.bank(scope) != row["scope"].get("__bank"):
        return "revoked", "policy_changed"   # namespace binding changed
    if row.get("delete_requested"):
        if not (service.current(scope) and scope.delete):
            return "revoked", "policy_changed"
        upstream = await service.client.operation(service.bank(scope), str(operation), authorize=lambda: service.current(scope))
        if failed(upstream):
            return ("revoked", "policy_changed") if upstream.status == 403 else ("retry", upstream.kind)
        if upstream.get("status") != "completed":
            return "retry", "remote_pending"
        deleted = await service.client.delete_document(service.bank(scope), str(operation), authorize=lambda: service.current(scope))
        if failed(deleted):
            return ("revoked", "policy_changed") if deleted.status == 403 else ("retry", deleted.kind)
        return "deleted", None
    from .memory import rejected_content
    payload = row["payload"]
    if not (service.cfg.retention_enabled and service.current(scope, write=True) and not rejected_content(payload["content"], service.cfg)):
        return "revoked", "policy_changed"
    sent = await service.client.retain(service.bank(scope), payload["content"], str(operation),
                                       confirmed=payload["confirmed"], provenance=payload.get("provenance"),
                                       **({"shared_observations": True} if payload.get("shared_observations") else {}),
                                       authorize=lambda: service.current(scope, write=True))
    if failed(sent):
        return ("revoked", "policy_changed") if sent.status == 403 else ("retry", sent.kind)
    return "accepted", None


async def step(service):
    """True when a job was processed, False when none was due, or a Problem from the database."""
    from .memory import Scope, count
    row = await service.outbox.outbox_claim(service.cfg.outbox_lease_s, service.cfg.outbox_attempts)
    if row is None:
        return False
    if failed(row):
        return row
    scope = Scope(**{k: v for k, v in row["scope"].items() if k in Scope.__dataclass_fields__})
    operation, lease = UUID(row["operation_id"]), UUID(row["lease"])
    status, error = await deliver(service, scope, operation, row)
    delay = 0
    if status == "retry":
        attempts = row["attempts"] - row.get("retry_base", 0)
        status = "dead" if attempts >= service.cfg.outbox_attempts else ("delete_pending" if row.get("delete_requested") else "pending")
        delay = min(3600, service.cfg.outbox_backoff_s * 2 ** min(attempts - 1, 12))
    finished = await service.outbox.outbox_finish(operation, lease, status, delay, error=error)
    if failed(finished):
        return finished
    count("outbox", status)
    return True


async def run(service):
    async with asyncio.TaskGroup() as group:
        for _ in range(service.cfg.outbox_workers):
            group.create_task(consume(service))


async def wait_for_wakeup(service, seconds):
    """Wait until woken or `seconds` passed. asyncio.wait reports the timeout as state, not as an exception."""
    waiter = asyncio.ensure_future(service.wakeup.wait())
    done, _ = await asyncio.wait({waiter}, timeout=seconds)
    if not done:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def consume(service):
    while True:
        if service.active():
            worked = await step(service)
            if worked is True:
                continue
            if failed(worked):
                outcome = "lease_lost" if worked.status == 409 else "database_unavailable"
                metrics._counter("skirnir_memory_events_total", {"operation": "outbox", "outcome": outcome})
        service.wakeup.clear()
        await wait_for_wakeup(service, 1)
