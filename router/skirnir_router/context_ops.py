"""Content-free service status, maintenance and aggregate gauges."""
import asyncio
import time

from aiohttp import web

from . import auth, memory, metrics, recall_skips, session_api, state
from .common import log, read_json
from .outcome import failed

API_KEY = "skirnir_context_api"
STATUS = {"session_store": "disabled", "context_manager": "disabled", "hindsight": "disabled", "outbox": "disabled", "outbox_counts": {}}


def snapshot():
    # recall_skipped_for_tools (ADR-0090): clients whose recent recalls were all skipped because the request carried
    # tools; read live, it is in memory and cheap.
    return {**STATUS, "recall_skipped_for_tools": recall_skips.view()}


async def poll(api):
    cfg = state.CFG.session_store
    store = api.get(session_api.STORE_KEY)
    STATUS.update(session_store="disabled", context_manager="enabled" if state.CFG.context_manager.enabled else "disabled",
                  hindsight="disabled", outbox="disabled", outbox_counts={}, outbox_oldest_seconds=0, checked_at=int(time.time()))
    if cfg.enabled and store is not None:
        STATUS["session_store"] = "unavailable" if failed(await store.health()) else "available"
    service = api.get(memory.SERVICE_KEY)
    if service is not None and service.active():
        healthy = await service.client.health()   # probed on every pass, not inferred from the last client call
        STATUS["hindsight"] = "unknown" if healthy is None else ("available" if healthy else "unavailable")
        if service.outbox is not None:
            counts, age = await service.outbox.outbox_counts(), await service.outbox.outbox_oldest()
            stuck = await service.outbox.outbox_stuck_deletions()
            if failed(counts) or failed(age) or failed(stuck):
                STATUS["outbox"] = "unavailable"
            else:
                STATUS["outbox_counts"] = counts
                STATUS["outbox_oldest_seconds"] = age if isinstance(age, (int, float)) else 0
                STATUS["outbox_stuck_deletions"] = int(stuck or 0)   # ADR-0076: requested but dead or revoked
                STATUS["outbox"] = "available"


RECONCILE_PAGE = 500


async def handle_reconcile_deletions(request):
    """POST /admin/memory/reconcile-deletions [{"after": "<operation id>", "limit": 1..1000}] (ADR-0076): after Hindsight
    was restored from a backup, delete again every document whose memory operation the router had deleted. One page per
    call in operation-id order; call again with `after` = `next` until `next` is null. A document Hindsight no longer has
    counts as deleted. Content-free answer: counts only."""
    api = request.app[API_KEY]
    service = api.get(memory.SERVICE_KEY)
    if service is None or service.outbox is None or not service.active():
        return web.json_response({"error": "durable memory outbox is not active"}, status=404)
    body = (await read_json(request) if request.can_read_body else None) or {}
    after = body.get("after", "00000000-0000-0000-0000-000000000000") if isinstance(body, dict) else None
    limit = body.get("limit", RECONCILE_PAGE) if isinstance(body, dict) else None
    after_id = session_api.uuid(after) if isinstance(after, str) else None
    if after_id is None or type(limit) is not int or not 1 <= limit <= 1000:
        return web.json_response({"error": "object with optional after (uuid) and limit (1..1000) expected"}, status=400)
    rows = await service.outbox.outbox_deleted_documents(after_id, limit)
    if failed(rows):
        return web.json_response({"error": "session store unavailable"}, status=503)
    deleted = unavailable = 0
    for operation, bank in rows:
        result = await service.client.delete_document(bank, str(operation)) if bank else None
        if result is None or failed(result):
            unavailable += 1
        else:
            deleted += 1
    nxt = str(rows[-1][0]) if len(rows) == limit else None
    auth.audit("memory_reconcile_deletions", by="admin-api", checked=len(rows), deleted=deleted, unavailable=unavailable)
    log.info("memory reconcile: %d checked, %d deleted or already gone, %d not reachable%s", len(rows), deleted, unavailable,
             "" if nxt is None else ", more follow")
    return web.json_response({"checked": len(rows), "deleted": deleted, "unavailable": unavailable, "next": nxt})


async def pause(stopping, seconds):
    """Wait `seconds` or until stopping. asyncio.wait reports the timeout as state, not as an exception."""
    waiter = asyncio.ensure_future(stopping.wait())
    done, _ = await asyncio.wait({waiter}, timeout=seconds)
    if not done:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def maintenance(api, stopping):
    # None: no cleanup yet, the first pass cleans up at once. A 0 here held the first cleanup back for
    # maintenance_interval_s after boot, because time.monotonic() starts near zero then.
    last_cleanup = None
    while not stopping.is_set():
        await poll(api)
        cfg = state.CFG.session_store
        memory_cfg = memory.settings()
        store = api.get(session_api.STORE_KEY)
        due = last_cleanup is None or time.monotonic() - last_cleanup >= cfg.maintenance_interval_s
        if cfg.enabled and store is not None and not stopping.is_set() and due:
            last_cleanup = time.monotonic()
            if cfg.retention_days or cfg.archived_retention_days or memory_cfg.outbox_retention_days:
                result = await store.cleanup(cfg.retention_days, cfg.archived_retention_days, memory_cfg.outbox_retention_days,
                                             with_memory=cfg.delete_memory_with_session)
                if failed(result):
                    metrics._counter("skirnir_context_maintenance_total", {"outcome": "unavailable"})
                else:
                    for kind, value in result.items():
                        metrics._counter("skirnir_context_cleanup_total", {"kind": kind}, value)
        await pause(stopping, 10)


async def lifecycle(api):
    task, stopping = None, asyncio.Event()
    STATUS.update(session_store="disabled", context_manager="disabled", hindsight="disabled", outbox="disabled", outbox_counts={}, outbox_oldest_seconds=0)
    if state.CFG.session_store.enabled or memory.settings().enabled:
        await poll(api)
        task = asyncio.create_task(maintenance(api, stopping))
    try:
        yield
    finally:
        if task:
            # Ordered stop before the session store closes its pool (cleanup contexts end in reverse order): a pass in
            # progress ends its transaction, at most session_store.timeout_s; only what still runs then is cancelled.
            stopping.set()
            done, _ = await asyncio.wait({task}, timeout=state.CFG.session_store.timeout_s)
            if not done:
                metrics._counter("skirnir_context_maintenance_total", {"outcome": "shutdown_cancel"})
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def handle(request):
    # Maintenance refreshes status; UI refreshes never scan the outbox ledger.
    return web.json_response(snapshot(), headers={"Cache-Control": "no-store"})
