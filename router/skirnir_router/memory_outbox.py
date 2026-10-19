"""Persistent retain submission and bounded multi-router retry worker with verified delivery.

A job is sent (pending -> processing -> accepted), then verified: the worker asks Hindsight whether
the extraction completed (accepted -> completed). An extraction Hindsight reports as failed - e.g.
because the local model was busy for hours - is resubmitted later under a new upstream operation id
with the same document id, a bounded number of times. Functions return their result or a Problem
(outcome.py); the worker records each outcome as a fixed state and diagnostic category on the row.
"""
import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID, uuid5

from . import metrics
from .outcome import denied, failed


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def upstream_id(operation, row):
    """Upstream operation id of the current send: the job's own UUID until Hindsight reported an extraction as failed,
    then a new id per resubmission (Hindsight answers a known id with its old state and would never retry it).
    The document id stays the job's UUID, so a resubmission replaces the same document and deletion keeps working."""
    attempt = row.get("upstream_attempts") or 0
    return str(operation) if attempt == 0 else str(uuid5(operation, f"resubmit-{attempt}"))


def verify_delay(cfg, row):
    return min(cfg.verify_max_delay_s, cfg.verify_delay_s * 2 ** min(row.get("verify_checks") or 0, 12))


def resubmit_delay(cfg, attempt):
    return min(21600, cfg.upstream_backoff_s * 2 ** min(attempt - 1, 12))


async def submit(service, scope, event_id, content, confirmed, provenance=None, shared_observations=False, context=None, timestamp=None):
    """Operation UUID admitted durably, or a Problem. `context` and `timestamp` (gateway retain, already checked there)
    are stored with the job but are not part of its fingerprint, so a replay with a newer send time is the same job."""
    operation = service.validate_retain(scope, event_id, content)
    if failed(operation):
        return operation
    from .memory import rejected_content
    from .memory_gateway import provenance as make_provenance
    origin = provenance or make_provenance({}, scope.principal, confirmed=confirmed)
    if failed(origin):
        return origin
    if rejected_content(json.dumps(origin), service.cfg, private_addresses=scope.private_addresses):
        return denied("sensitive provenance")
    payload = {"content": content, "confirmed": confirmed, "provenance": origin}
    if shared_observations:
        payload["shared_observations"] = True
    # Optional keys only when given: rows without them (also all rows admitted before) keep their shape and fingerprint.
    item = {key: value for key, value in (("context", context), ("timestamp", timestamp)) if value is not None}
    admitted = await service.outbox.outbox_put(UUID(operation), {**asdict(scope), "__bank": service.bank(scope)}, {**payload, **item}, digest(payload),
                                               service.cfg.outbox_capacity, **({"owner_capacity": service.cfg.outbox_client_capacity} if service.cfg.outbox_client_capacity else {}))
    if failed(admitted):
        return admitted
    service.wakeup.set()
    return operation


async def deliver(service, scope, operation, row):
    """Hand one claimed send to Hindsight. (status, error category) for the outbox row."""
    if service.bank(scope) != row["scope"].get("__bank"):
        return "revoked", "policy_changed"   # namespace binding changed
    if row.get("delete_requested"):
        if not (service.current(scope) and scope.delete):
            return "revoked", "policy_changed"
        upstream = await service.client.operation(service.bank(scope), upstream_id(operation, row), authorize=lambda: service.current(scope))
        if failed(upstream):
            return ("revoked", "policy_changed") if upstream.status == 403 else ("retry", upstream.kind)
        # Delete only when no write can follow: the extraction is completed, or Hindsight gave up on it.
        if upstream.get("status") not in ("completed", "failed"):
            return "retry", "remote_pending"
        deleted = await service.client.delete_document(service.bank(scope), str(operation), authorize=lambda: service.current(scope))
        if failed(deleted):
            return ("revoked", "policy_changed") if deleted.status == 403 else ("retry", deleted.kind)
        return "deleted", None
    from .memory import rejected_content
    payload = row["payload"]
    if not (service.cfg.retention_enabled and service.current(scope, write=True)
            and not rejected_content(payload["content"], service.cfg, private_addresses=scope.private_addresses)
            and not rejected_content(payload.get("context", ""), service.cfg, private_addresses=scope.private_addresses)):
        return "revoked", "policy_changed"
    sent = await service.client.retain(service.bank(scope), payload["content"], upstream_id(operation, row), document_id=str(operation),
                                       confirmed=payload["confirmed"], provenance=payload.get("provenance"),
                                       **({"shared_observations": True} if payload.get("shared_observations") else {}),
                                       context=payload.get("context"), timestamp=payload.get("timestamp"),
                                       authorize=lambda: service.current(scope, write=True))
    if failed(sent):
        return ("revoked", "policy_changed") if sent.status == 403 else ("retry", sent.kind)
    return "accepted", None


async def verify(service, scope, operation, row):
    """Ask Hindsight whether the accepted extraction completed. (status, error category, delay, upstream_attempts)."""
    cfg = service.cfg
    if service.bank(scope) != row["scope"].get("__bank"):
        return "revoked", "policy_changed", 0, None
    upstream = await service.client.operation(service.bank(scope), upstream_id(operation, row), authorize=lambda: service.current(scope))
    if failed(upstream):
        if upstream.status == 403:
            return "revoked", "policy_changed", 0, None
        return "accepted", upstream.kind, verify_delay(cfg, row), None   # status not readable right now: check again later
    status = upstream.get("status")
    if status == "completed":
        return "completed", None, 0, None
    if status in ("failed", "cancelled"):
        attempt = (row.get("upstream_attempts") or 0) + 1
        if attempt - (row.get("upstream_base") or 0) > cfg.upstream_attempts:
            return "dead", "upstream_failed", 0, None
        return "pending", "upstream_failed", resubmit_delay(cfg, attempt - (row.get("upstream_base") or 0)), attempt
    if status == "not_found":   # Hindsight no longer knows the operation: completion cannot be confirmed
        return "dead", "remote_unknown", 0, None
    # The value comes from our own record(): always an ISO timestamp with time zone once the job was accepted.
    accepted_at = datetime.fromisoformat(row["accepted_at"])
    if (datetime.now(timezone.utc) - accepted_at).total_seconds() > cfg.verify_timeout_s:
        return "dead", "remote_pending", 0, None
    return "accepted", None, verify_delay(cfg, row), None


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
    attempts = None
    if row["state"] == "accepted":
        status, error, delay, attempts = await verify(service, scope, operation, row)
    else:
        status, error = await deliver(service, scope, operation, row)
        delay = service.cfg.verify_delay_s if status == "accepted" else 0
        if status == "retry":
            sends = row["attempts"] - row.get("retry_base", 0)
            status = "dead" if sends >= service.cfg.outbox_attempts else ("delete_pending" if row.get("delete_requested") else "pending")
            delay = min(3600, service.cfg.outbox_backoff_s * 2 ** min(sends - 1, 12))
    finished = await service.outbox.outbox_finish(operation, lease, status, delay, error=error,
                                                  **({"upstream_attempts": attempts} if attempts is not None else {}))
    if failed(finished):
        return finished
    if status != row["state"]:
        count("outbox", status)
    return True


async def run(service):
    async with asyncio.TaskGroup() as group:
        for _ in range(service.cfg.outbox_workers):
            group.create_task(consume(service))


async def wait_for_wakeup(service, seconds):
    """Wait until woken, stopping or `seconds` passed. asyncio.wait reports the timeout as state, not as an exception."""
    waiters = {asyncio.ensure_future(service.wakeup.wait()), asyncio.ensure_future(service.stopping.wait())}
    _, pending = await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    for waiter in pending:
        waiter.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def consume(service):
    # Ends after the current job once the service is stopping (MemoryService.close): its transactions complete and its
    # outcome is recorded, instead of a cancellation cutting them off.
    while not service.stopping.is_set():
        if service.active():
            worked = await step(service)
            if worked is True:
                continue
            if failed(worked):
                outcome = "lease_lost" if worked.status == 409 else "database_unavailable"
                metrics._counter("skirnir_memory_events_total", {"operation": "outbox", "outcome": outcome})
        service.wakeup.clear()
        await wait_for_wakeup(service, 1)
