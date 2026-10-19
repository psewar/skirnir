"""Content-free service status, maintenance and aggregate gauges."""
import asyncio
import time

from aiohttp import web

from . import memory, metrics, session_api, state
from .outcome import failed

API_KEY = "skirnir_context_api"
STATUS = {"session_store": "disabled", "context_manager": "disabled", "hindsight": "disabled", "outbox": "disabled", "outbox_counts": {}}


def snapshot():
    return dict(STATUS)


async def poll(api):
    cfg = state.CFG.session_store
    store = api.get(session_api.STORE_KEY)
    STATUS.update(session_store="disabled", context_manager="enabled" if state.CFG.context_manager.enabled else "disabled",
                  hindsight="disabled", outbox="disabled", outbox_counts={}, outbox_oldest_seconds=0, checked_at=int(time.time()))
    if cfg.enabled and store is not None:
        STATUS["session_store"] = "unavailable" if failed(await store.health()) else "available"
    service = api.get(memory.SERVICE_KEY)
    if service is not None and service.active():
        healthy = service.client.healthy
        STATUS["hindsight"] = "unknown" if healthy is None else ("available" if healthy else "unavailable")
        if service.outbox is not None:
            counts, age = await service.outbox.outbox_counts(), await service.outbox.outbox_oldest()
            if failed(counts) or failed(age):
                STATUS["outbox"] = "unavailable"
            else:
                STATUS["outbox_counts"] = counts
                STATUS["outbox_oldest_seconds"] = age if isinstance(age, (int, float)) else 0
                STATUS["outbox"] = "available"


async def pause(stopping, seconds):
    """Wait `seconds` or until stopping. asyncio.wait reports the timeout as state, not as an exception."""
    waiter = asyncio.ensure_future(stopping.wait())
    done, _ = await asyncio.wait({waiter}, timeout=seconds)
    if not done:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def maintenance(api, stopping):
    last_cleanup = 0
    while not stopping.is_set():
        await poll(api)
        cfg = state.CFG.session_store
        memory_cfg = memory.settings()
        store = api.get(session_api.STORE_KEY)
        if (cfg.enabled and store is not None and not stopping.is_set()
                and time.monotonic() - last_cleanup >= cfg.maintenance_interval_s):
            last_cleanup = time.monotonic()
            if cfg.retention_days or cfg.archived_retention_days or memory_cfg.outbox_retention_days:
                result = await store.cleanup(cfg.retention_days, cfg.archived_retention_days, memory_cfg.outbox_retention_days)
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
