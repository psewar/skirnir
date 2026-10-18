"""Authenticated explicit session API. No inference, recall or compaction side effects."""

import json
import os
import time
from uuid import UUID

from aiohttp import web

from . import auth, metrics, state
from .session_config import SessionConfig
from .context_config import ContextConfig
from .session_store import PostgresSessionStore, SessionConflict, SessionNotFound, SessionUnavailable, SessionCapacity

STORE_KEY = "skirnir_session_store"  # string keys support aiohttp 3.8 (router minimum)
CONFIG_KEY = "skirnir_session_store_config"
MAX_BODY_BYTES = 1024 * 1024
PREFIX = "/v1/skirnir/sessions"


async def lifecycle(app):
    cfg = app[CONFIG_KEY]
    store = None
    if cfg.enabled:
        dsn = os.environ.get(cfg.dsn_env)
        if not dsn:
            raise RuntimeError("session store requires its configured DSN environment variable")
        try:
            store = await PostgresSessionStore.open(dsn, cfg)
        except (ImportError, SessionUnavailable):
            raise RuntimeError("session store startup failed; check driver, database and credentials") from None
    app[STORE_KEY] = store
    try:
        yield
    finally:
        if store is not None:
            await store.close()


def register(app):
    app[CONFIG_KEY] = getattr(state.CFG, "session_store", SessionConfig())
    app.cleanup_ctx.append(lifecycle)
    app.router.add_post(PREFIX, handle, name="session-create")
    app.router.add_get(PREFIX + "/{session_id}", handle, name="session-get")
    app.router.add_patch(PREFIX + "/{session_id}", handle, name="session-update")
    app.router.add_delete(PREFIX + "/{session_id}", handle, name="session-delete")
    for resource in ("messages", "checkpoints"):
        app.router.add_get(PREFIX + "/{session_id}/" + resource, handle, name="session-read-" + resource)
        app.router.add_post(PREFIX + "/{session_id}/" + resource, handle, name="session-append-" + resource)
    for resource in ("compactions", "snapshot", "search"):
        app.router.add_get(PREFIX + "/{session_id}/" + resource, handle, name="session-" + resource)


def fail(status, message):
    return web.json_response({"error": {"message": message, "type": "session_error"}}, status=status,
                             headers={"Cache-Control": "no-store"})


def text(value, name, max_len=128):
    if not isinstance(value, str) or not value.strip() or len(value) > max_len or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a nonempty string of at most {max_len} characters")
    return value


def fields(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise ValueError("invalid object fields")


async def body(request):
    # Bound before buffering; API app otherwise accepts 64 MiB for inference images.
    data = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY_BYTES, actual_size=len(data))
    try:
        def invalid_constant(_):
            raise ValueError("non-finite JSON number")
        value = json.loads(data, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("body must contain valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("body must be an object")
    return value


def allowed_project(principal, project):
    projects = (auth.client_auth_cfg()["clients"].get(principal) or {}).get("session_projects", [])
    return project in projects or "*" in projects


def cursor(request, extra=()):
    if set(request.query) - {"after", "limit", *extra}:
        raise ValueError("unknown pagination parameter")
    try:
        after, limit = int(request.query.get("after", "0")), int(request.query.get("limit", "50"))
    except ValueError:
        raise ValueError("after and limit must be integers") from None
    if not 0 <= after <= 9223372036854775807 or not 1 <= limit <= 100:
        raise ValueError("after must be nonnegative and limit between 1 and 100")
    return after, limit


def validate_payload(data, kind):
    fields(data, ("event_id", kind), ("event_id", kind))
    text(data["event_id"], "event_id")
    payload = data[kind]
    if not isinstance(payload, dict):
        raise ValueError(f"{kind} must be an object")
    if kind == "message":
        if payload.get("role") not in ("system", "developer", "user", "assistant", "tool", "function"):
            raise ValueError("message.role is required")
        # Keep original protocol-specific content/tool-call fields, without conversion.
    else:
        fields(payload, ("schema_version", "goals", "open_steps", "artifacts", "last_result", "provenance"), ("schema_version",))
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("checkpoint.schema_version must be 1")
        for key in ("goals", "open_steps", "artifacts"):
            if key in payload and (not isinstance(payload[key], list) or any(not isinstance(x, str) for x in payload[key])):
                raise ValueError(f"checkpoint.{key} must be a list of strings")
        if "last_result" in payload and not isinstance(payload["last_result"], str):
            raise ValueError("checkpoint.last_result must be a string")
        if "provenance" in payload and not isinstance(payload["provenance"], dict):
            raise ValueError("checkpoint.provenance must be an object")
    return data["event_id"], payload


async def handle(request):
    action = request.match_info.route.name.removeprefix("session-")
    started = time.monotonic()
    response = None
    try:
        cfg = getattr(state.CFG, "session_store", SessionConfig())
        if not cfg.enabled or not request.app[CONFIG_KEY].enabled:
            response = fail(404, "session store disabled (configuration changes require restart)")
        else:
            response = await dispatch(request, action)
    except SessionCapacity:
        response = fail(413, "session working set exceeds configured limit")
    except SessionNotFound:
        response = fail(404, "session not found")
    except SessionConflict as exc:
        response = fail(409, str(exc))
    except SessionUnavailable:
        response = fail(503, "session store unavailable; retry with the same event_id")
    except (ValueError, UnicodeError, RecursionError):
        response = fail(400, "invalid session request")
    except web.HTTPRequestEntityTooLarge:
        response = fail(413, "session body exceeds 1 MiB")
    finally:
        status = str(response.status) if response is not None else "cancelled"
        metrics._counter("skirnir_session_requests_total", {"operation": action, "status": status})
        metrics._hist("skirnir_session_duration_seconds", {"operation": action}, metrics.DURATION_BUCKETS, time.monotonic() - started)
    response.headers["Cache-Control"] = "no-store"
    return response


async def dispatch(request, action):
    # Even observe mode/IP-only clients cannot access stored conversations. Use
    # existing credential resolver; session_id/project never establish identity.
    principal, via = auth.resolve_client(request)
    if principal is None or via not in ("bearer", "basic", "x-api-key") or principal != request.get("client"):
        return auth._deny(request, 401, "session API requires authenticated client credentials")
    if not allowed_project(principal, "*") and not auth.client_auth_cfg()["clients"][principal].get("session_projects"):
        return fail(403, "session access is not enabled for this client")
    store = request.app[STORE_KEY]
    if store is None:
        raise SessionUnavailable
    if action == "create":
        data = await body(request)
        fields(data, ("project", "metadata", "mode"), ("project",))
        project = text(data["project"], "project")
        if not allowed_project(principal, project):
            return fail(403, "project not allowed")
        mode = data.get("mode", "client-managed")
        if mode not in ("client-managed", "skirnir-managed"):
            raise ValueError("invalid session mode")
        if mode == "skirnir-managed" and not getattr(state.CFG, "context_manager", ContextConfig()).enabled:
            raise ValueError("managed context is disabled")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        client = auth.client_auth_cfg()["clients"][principal]
        data_class = client.get("data_class") or state.CFG.cloud["default_data_class"]
        result = (await store.create(principal, project, data_class, metadata, mode=mode) if mode == "skirnir-managed"
                  else await store.create(principal, project, data_class, metadata))
        return web.json_response(result, status=201)
    try:
        session_id = UUID(request.match_info["session_id"])
    except ValueError:
        raise SessionNotFound from None
    session = await store.get(principal, session_id)
    if not allowed_project(principal, session["project"]):
        raise SessionNotFound
    if action == "get":
        return web.json_response(session)
    if action == "delete":
        await store.delete(principal, session_id)
        return web.Response(status=204)
    if action == "update":
        data = await body(request)
        fields(data, ("status", "metadata"))
        if not data or ("status" in data and data["status"] not in ("active", "archived")):
            raise ValueError("invalid status")
        if "metadata" in data and not isinstance(data["metadata"], dict):
            raise ValueError("metadata must be an object")
        return web.json_response(await store.update(principal, session_id, data.get("status"), data.get("metadata")))
    if action in ("compactions", "snapshot", "search"):
        return await context_resource(request, store, principal, session_id, action)
    kind = "message" if action.endswith("messages") else "checkpoint"
    if action.startswith("append-"):
        event_id, payload = validate_payload(await body(request), kind)
        event, created = await store.append(principal, session_id, event_id, kind, payload)
        return web.json_response(event, status=201 if created else 200)
    after, limit = cursor(request)
    items = await store.read(principal, session_id, kind, after, limit)
    return web.json_response({"items": items, "next_after": items[-1]["seq"] if items else after})


async def context_resource(request, store, owner, sid, action):
    if action == "snapshot":
        if request.query:
            raise ValueError("snapshot has no query parameters")
        from .context_manager import compose
        context = await store.context(owner, sid, getattr(state.CFG, "context_manager", ContextConfig()))
        # This is an active-context export, never a promise to resume OS processes.
        return web.json_response({"schema_version": 1, "kind": "skirnir-context-snapshot", "resumable_execution": False,
            "provenance": {"session_id": str(sid), "project": context["session"]["project"],
                           "data_class": context["session"]["data_class"], "mode": context["session"]["mode"],
                           "compaction_version": context["compaction"]["version"] if context["compaction"] else None,
                           "message_head": context["messages"][-1]["seq"] if context["messages"] else (context["compaction"]["through_seq"] if context["compaction"] else 0)},
            "last_turn": context.get("last_turn"),
            "messages": compose(context), "compaction": context["compaction"], "checkpoint": context["checkpoint"],
            "raw_history_complete": context["compaction"] is None})
    if action == "search":
        if set(request.query) - {"q", "after", "limit"}:
            raise ValueError("unknown search parameter")
        query = text(request.query.get("q"), "q", 256)
        after, limit = cursor(request, ("q",))
        items = await store.search(owner, sid, query, after, limit)
        return web.json_response({"items": items, "next_after": items[-1]["seq"] if items else after})
    after, limit = cursor(request)
    items = await store.compactions(owner, sid, after, limit)
    return web.json_response({"items": items, "next_after": items[-1]["version"] if items else after})
