"""Content-free service status, maintenance and aggregate gauges."""
import asyncio
import time

from aiohttp import web

from . import memory, metrics, session_api, state
from .session_types import SessionUnavailable, SessionConflict

API_KEY = "skirnir_context_api"
STATUS = {"session_store": "disabled", "context_manager": "disabled", "hindsight": "disabled", "outbox": "disabled", "outbox_counts": {}}


def snapshot():
    return dict(STATUS)


async def poll(api):
    cfg = state.CFG.session_store
    store = api.get(session_api.STORE_KEY)
    STATUS.update(session_store="disabled", context_manager="enabled" if state.CFG.context_manager.enabled else "disabled",
                  hindsight="disabled", outbox="disabled", outbox_counts={}, checked_at=int(time.time()))
    if cfg.enabled and store is not None:
        try:
            await store.health()
            STATUS["session_store"] = "available"
        except SessionUnavailable:
            STATUS["session_store"] = "unavailable"
    service = api.get(memory.SERVICE_KEY)
    if service is not None and service.active():
        healthy = service.client.healthy
        STATUS["hindsight"] = "unknown" if healthy is None else ("available" if healthy else "unavailable")
        if service.outbox is not None:
            try:
                STATUS["outbox_counts"] = await service.outbox.outbox_counts()
                STATUS["outbox"] = "available"
            except SessionUnavailable:
                STATUS["outbox"] = "unavailable"


async def maintenance(api):
    last_cleanup = 0
    while True:
        try:
            await poll(api)
            cfg = state.CFG.session_store
            memory_cfg = memory.settings()
            store = api.get(session_api.STORE_KEY)
            if cfg.enabled and store is not None and time.monotonic() - last_cleanup >= cfg.maintenance_interval_s:
                last_cleanup = time.monotonic()
                if cfg.retention_days or cfg.archived_retention_days or memory_cfg.outbox_retention_days:
                    result = await store.cleanup(cfg.retention_days, cfg.archived_retention_days, memory_cfg.outbox_retention_days)
                    for kind, value in result.items():
                        metrics._counter("skirnir_context_cleanup_total", {"kind": kind}, value)
        except (SessionUnavailable, SessionConflict):
            metrics._counter("skirnir_context_maintenance_total", {"outcome": "unavailable"})
        await asyncio.sleep(10)


async def lifecycle(api):
    task = None
    STATUS.update(session_store="disabled", context_manager="disabled", hindsight="disabled", outbox="disabled", outbox_counts={})
    if state.CFG.session_store.enabled or memory.settings().enabled:
        task = asyncio.create_task(maintenance(api))
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def handle(request):
    await poll(request.app[API_KEY])
    return web.json_response(snapshot(), headers={"Cache-Control": "no-store"})
