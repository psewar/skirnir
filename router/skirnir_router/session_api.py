"""Authenticated explicit session API. No inference, recall or compaction side effects.

Validation helpers return the value or a Problem (outcome.py); handlers answer a Problem
with its own status and fixed message.
"""

import json
import os
import re
import time
from uuid import UUID

from aiohttp import web

from . import auth, metrics, state
from .outcome import Problem, failed, invalid, too_large
from .session_config import SessionConfig
from .context_config import ContextConfig
from .session_store import PostgresSessionStore
from .session_types import NOT_FOUND, UNAVAILABLE

STORE_KEY = "skirnir_session_store"  # string keys support aiohttp 3.8 (router minimum)
CONFIG_KEY = "skirnir_session_store_config"
MAX_BODY_BYTES = 1024 * 1024
PREFIX = "/v1/skirnir/sessions"
UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
COUNT_TEXT = re.compile(r"[0-9]{1,19}")
BODY_TOO_LARGE = too_large("session body exceeds 1 MiB")
INVALID = invalid("invalid session request")


async def lifecycle(app):
    cfg = app[CONFIG_KEY]
    store = None
    if cfg.enabled:
        # aiohttp aborts startup only through an exception from a cleanup context: these
        # RuntimeErrors stop the router with the reason; nothing catches them to branch.
        if cfg.handoff_enabled and len(os.environ.get(cfg.handoff_key_env, "").encode()) < 32:
            raise RuntimeError("session handoffs require a signing key of at least 32 bytes")  # exception-ok: aborts startup
        dsn = os.environ.get(cfg.dsn_env)
        if not dsn:
            raise RuntimeError("session store requires its configured DSN environment variable")  # exception-ok: aborts startup
        store = await PostgresSessionStore.open(dsn, cfg)
        if failed(store):
            raise RuntimeError("session store startup failed; check driver, database and credentials")  # exception-ok: aborts startup
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
    app.router.add_get(PREFIX, handle, name="session-list")
    app.router.add_post(PREFIX + "/import", handle, name="session-handoff-imported")
    app.router.add_get(PREFIX + "/{session_id}/checkpoints/latest", handle, name="session-latest-checkpoint")
    app.router.add_get(PREFIX + "/{session_id}", handle, name="session-get")
    app.router.add_patch(PREFIX + "/{session_id}", handle, name="session-update")
    app.router.add_delete(PREFIX + "/{session_id}", handle, name="session-delete")
    for resource in ("messages", "checkpoints"):
        app.router.add_get(PREFIX + "/{session_id}/" + resource, handle, name="session-read-" + resource)
        app.router.add_post(PREFIX + "/{session_id}/" + resource, handle, name="session-append-" + resource)
    for resource in ("compactions", "snapshot", "search", "handoff"):
        app.router.add_get(PREFIX + "/{session_id}/" + resource, handle, name="session-" + resource)


def fail(status, message):
    return web.json_response({"error": {"message": message, "type": "session_error"}}, status=status,
                             headers={"Cache-Control": "no-store"})


def answer(problem):
    return fail(problem.status, problem.message)


def text(value, name, max_len=128):
    """The string, or a Problem: nonempty, at most max_len characters, no control characters."""
    if not isinstance(value, str) or not value.strip() or len(value) > max_len or any(ord(c) < 32 for c in value):
        return invalid(f"{name} must be a nonempty string of at most {max_len} characters")
    return value


def fields(data, allowed, required=()):
    """None, or a Problem when data is not an object with only allowed and all required fields."""
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        return invalid("invalid object fields")
    return None


def uuid(value):
    """UUID from its text form, or None. The pattern is checked first, so UUID() never fails."""
    return UUID(value) if isinstance(value, str) and UUID_TEXT.fullmatch(value) else None


def count(value, low, high):
    """Integer within [low, high] from its decimal text form, or None."""
    if not isinstance(value, str) or not COUNT_TEXT.fullmatch(value):
        return None
    number = int(value)
    return number if low <= number <= high else None


def parse_json_value(data):
    """Any JSON value from bytes or text, or a Problem. Rejects NaN/Infinity, which strict JSON does not allow."""
    constants = []
    # Parsing is the only complete validity check for JSON: the parser's exception is its
    # report on this untrusted input (syntax, encoding, nesting depth), caught right here.
    try:
        value = json.loads(data, parse_constant=constants.append)
    except (ValueError, RecursionError):  # exception-ok: JSON validity is only known by parsing
        return invalid("body must contain valid JSON")
    return invalid("body must contain valid JSON") if constants else value


def parse_json(data):
    """JSON object from bytes, or a Problem."""
    value = parse_json_value(data)
    if not failed(value) and not isinstance(value, dict):
        return invalid("body must be an object")
    return value


async def body(request):
    # Bound before buffering; API app otherwise accepts 64 MiB for inference images.
    data = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > MAX_BODY_BYTES:
            return BODY_TOO_LARGE
    return parse_json(data)


def allowed_project(principal, project):
    projects = (auth.client_auth_cfg()["clients"].get(principal) or {}).get("session_projects", [])
    return project in projects or "*" in projects


def cursor(request, extra=()):
    """(after, limit) from the query, or a Problem."""
    if set(request.query) - {"after", "limit", *extra}:
        return invalid("unknown pagination parameter")
    after = count(request.query.get("after", "0"), 0, 9223372036854775807)
    limit = count(request.query.get("limit", "50"), 1, 100)
    if after is None or limit is None:
        return invalid("after must be nonnegative and limit between 1 and 100")
    return after, limit


def validate_payload(data, kind):
    """(event_id, payload), or a Problem."""
    if failed(problem := fields(data, ("event_id", kind), ("event_id", kind))):
        return problem
    if failed(problem := text(data["event_id"], "event_id")):
        return problem
    payload = data[kind]
    if not isinstance(payload, dict):
        return invalid(f"{kind} must be an object")
    if kind == "message":
        if payload.get("role") not in ("system", "developer", "user", "assistant", "tool", "function"):
            return invalid("message.role is required")
        # Keep original protocol-specific content/tool-call fields, without conversion.
    else:
        if failed(problem := fields(payload, ("schema_version", "goals", "open_steps", "artifacts", "last_result", "provenance"), ("schema_version",))):
            return problem
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            return invalid("checkpoint.schema_version must be 1")
        for key in ("goals", "open_steps", "artifacts"):
            if key in payload and (not isinstance(payload[key], list) or any(not isinstance(x, str) for x in payload[key])):
                return invalid(f"checkpoint.{key} must be a list of strings")
        if "last_result" in payload and not isinstance(payload["last_result"], str):
            return invalid("checkpoint.last_result must be a string")
        if "provenance" in payload and not isinstance(payload["provenance"], dict):
            return invalid("checkpoint.provenance must be an object")
    return data["event_id"], payload


# Client-facing wording per status. 400 and 404 stay generic on purpose: they must not reveal
# which check failed or whether a foreign session exists. 409 and 413 name the actual condition.
PUBLIC = {400: INVALID.message, 403: "session handoff or project permission denied", 404: NOT_FOUND.message,
          503: UNAVAILABLE.message}


async def handle(request):
    action = request.match_info.route.name.removeprefix("session-")
    started = time.monotonic()
    response = None
    try:
        cfg = getattr(state.CFG, "session_store", SessionConfig())
        if not cfg.enabled or not request.app[CONFIG_KEY].enabled:
            response = fail(404, "session store disabled (configuration changes require restart)")
        else:
            result = await dispatch(request, action)
            if isinstance(result, Problem):
                result = fail(result.status, result.message if result.status in (401, 409, 413) else PUBLIC.get(result.status, result.message))
            response = result
    finally:
        status = str(response.status) if response is not None else "cancelled"
        metrics._counter("skirnir_session_requests_total", {"operation": action, "status": status})
        metrics._hist("skirnir_session_duration_seconds", {"operation": action}, metrics.DURATION_BUCKETS, time.monotonic() - started)
    response.headers["Cache-Control"] = "no-store"
    return response


async def dispatch(request, action):
    """Response, or a Problem that handle() answers."""
    # Even observe mode/IP-only clients cannot access stored conversations. Use
    # existing credential resolver; session_id/project never establish identity.
    principal, via = auth.resolve_client(request)
    if principal is None or via not in ("bearer", "basic", "x-api-key") or principal != request.get("client"):
        return auth._deny(request, 401, "session API requires authenticated client credentials")
    if not allowed_project(principal, "*") and not auth.client_auth_cfg()["clients"][principal].get("session_projects"):
        return fail(403, "session access is not enabled for this client")
    store = request.app[STORE_KEY]
    if store is None:
        return UNAVAILABLE
    if action in ("list", "handoff-imported"):
        from . import session_exchange
        if action == "list":
            return await session_exchange.discover(request, store, principal)
        response = await session_exchange.import_package(request, store, principal)
        session_exchange.count("import", "error" if failed(response) else "ok")
        return response
    if action == "create":
        data = await body(request)
        if failed(data):
            return data
        if failed(problem := fields(data, ("project", "metadata", "mode"), ("project",))):
            return problem
        project = text(data["project"], "project")
        if failed(project):
            return project
        if not allowed_project(principal, project):
            return fail(403, "project not allowed")
        mode = data.get("mode", "client-managed")
        if mode not in ("client-managed", "skirnir-managed"):
            return invalid("invalid session mode")
        if mode == "skirnir-managed" and not getattr(state.CFG, "context_manager", ContextConfig()).enabled:
            return invalid("managed context is disabled")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            return invalid("metadata must be an object")
        client = auth.client_auth_cfg()["clients"][principal]
        data_class = client.get("data_class") or state.CFG.cloud["default_data_class"]
        result = (await store.create(principal, project, data_class, metadata, mode=mode) if mode == "skirnir-managed"
                  else await store.create(principal, project, data_class, metadata))
        return result if failed(result) else web.json_response(result, status=201)
    session_id = uuid(request.match_info["session_id"])
    if session_id is None:
        return NOT_FOUND
    session = await store.get(principal, session_id)
    if failed(session):
        return session
    if not allowed_project(principal, session["project"]):
        return NOT_FOUND
    if action == "get":
        return web.json_response(session)
    if action == "delete":
        deleted = await store.delete(principal, session_id)
        return deleted if failed(deleted) else web.Response(status=204)
    if action == "update":
        data = await body(request)
        if failed(data):
            return data
        if failed(problem := fields(data, ("status", "metadata"))):
            return problem
        if not data or ("status" in data and data["status"] not in ("active", "archived")):
            return invalid("invalid status")
        if "metadata" in data and not isinstance(data["metadata"], dict):
            return invalid("metadata must be an object")
        updated = await store.update(principal, session_id, data.get("status"), data.get("metadata"))
        return updated if failed(updated) else web.json_response(updated)
    if action == "handoff":
        from . import session_exchange
        package = await session_exchange.export(store, principal, session_id)
        session_exchange.count("export", "error" if failed(package) else "ok")
        return package if failed(package) else web.json_response(package)
    if action == "latest-checkpoint":
        context = await store.context(principal, session_id, getattr(state.CFG, "context_manager", ContextConfig()))
        return context if failed(context) else web.json_response({"checkpoint": context["checkpoint"], "resumable_execution": False})
    if action in ("compactions", "snapshot", "search"):
        return await context_resource(request, store, principal, session_id, action)
    kind = "message" if action.endswith("messages") else "checkpoint"
    if action.startswith("append-"):
        data = await body(request)
        if failed(data):
            return data
        validated = validate_payload(data, kind)
        if failed(validated):
            return validated
        event_id, payload = validated
        appended = await store.append(principal, session_id, event_id, kind, payload)
        if failed(appended):
            return appended
        event, created = appended
        return web.json_response(event, status=201 if created else 200)
    page = cursor(request)
    if failed(page):
        return page
    after, limit = page
    items = await store.read(principal, session_id, kind, after, limit)
    if failed(items):
        return items
    return web.json_response({"items": items, "next_after": items[-1]["seq"] if items else after})


async def context_resource(request, store, owner, sid, action):
    if action == "snapshot":
        if request.query:
            return invalid("snapshot has no query parameters")
        from .context_manager import compose
        context = await store.context(owner, sid, getattr(state.CFG, "context_manager", ContextConfig()))
        if failed(context):
            return context
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
            return invalid("unknown search parameter")
        query = text(request.query.get("q"), "q", 256)
        if failed(query):
            return query
        page = cursor(request, ("q",))
        if failed(page):
            return page
        after, limit = page
        items = await store.search(owner, sid, query, after, limit)
        if failed(items):
            return items
        return web.json_response({"items": items, "next_after": items[-1]["seq"] if items else after})
    page = cursor(request)
    if failed(page):
        return page
    after, limit = page
    items = await store.compactions(owner, sid, after, limit)
    if failed(items):
        return items
    return web.json_response({"items": items, "next_after": items[-1]["version"] if items else after})
