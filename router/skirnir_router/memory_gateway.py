"""Allowlisted Hindsight recall/retain compatibility gateway and operation control.

The path bank is a configured project alias, not an upstream identifier. No arbitrary
proxying, reflect, bank administration, payload/status disclosure or tenant selection.
"""
import hashlib
import json
from uuid import UUID

from aiohttp import web

from . import auth, memory, session_api
from .hindsight import MemoryUnavailable
from .session_types import SessionUnavailable, SessionConflict, SessionCapacity, SessionNotFound

GATEWAY = "/v1/skirnir/hindsight"


def provenance(raw, principal, *, confirmed):
    session_api.fields(raw, ("source_refs", "kind", "version", "supersedes"))
    refs = raw.get("source_refs", [])
    if not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(x, str) or len(x) > 256 for x in refs):
        raise ValueError("invalid provenance references")
    kind = raw.get("kind", "confirmed_information" if confirmed else "assistant_derivation")
    if kind not in ("user_assertion", "confirmed_information", "assistant_derivation") or (not confirmed and kind == "confirmed_information"):
        raise ValueError("invalid knowledge kind")
    result = {"kind": kind, "origin_client": principal, "source_refs": json.dumps(refs), "confirmed": str(confirmed).lower()}
    # Wall-clock time is added on dispatch; it is not part of an event fingerprint.
    for key in ("version", "supersedes"):
        if key in raw:
            result[key] = session_api.text(raw[key], key)
    return result


def register(app):
    for suffix in ("/version", "/v1/version"):
        app.router.add_get(GATEWAY + suffix, handle)
    base = GATEWAY + "/v1/default/banks/{alias}"
    app.router.add_post(base + "/memories/recall", handle)
    app.router.add_post(base + "/memories", handle)
    app.router.add_get(base + "/operations/{operation_id}", handle)
    app.router.add_get(memory.PREFIX + "/operations/{operation_id}", handle)
    app.router.add_delete(memory.PREFIX + "/operations/{operation_id}", handle)
    app.router.add_post(memory.PREFIX + "/operations/{operation_id}/delete", handle)
    app.router.add_post(memory.PREFIX + "/operations/{operation_id}/retry", handle)


async def handle(request):
    try:
        response = await dispatch(request)
    except memory.MemoryUnauthenticated:
        response = auth._deny(request, 401, "memory credentials required")
    except memory.MemoryDenied:
        response = session_api.fail(403, "memory scope denied")
    except SessionNotFound:
        response = session_api.fail(404, "memory operation not found")
    except (SessionConflict, memory.MemoryConflict):
        response = session_api.fail(409, "memory operation conflicts or may already be processing upstream")
    except (SessionUnavailable, SessionCapacity, MemoryUnavailable):
        response = session_api.fail(503, "memory backend unavailable")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        response = session_api.fail(400, "invalid memory gateway request")
    except web.HTTPRequestEntityTooLarge:
        response = session_api.fail(413, "memory request too large")
    response.headers["Cache-Control"] = "no-store"
    return response


async def dispatch(request):
    service = request.app.get(memory.SERVICE_KEY)
    gateway = request.path.startswith(GATEWAY + "/")
    if service is None or not service.active() or (gateway and not service.cfg.gateway_enabled):
        return session_api.fail(404, "memory gateway disabled")
    principal, via = auth.resolve_client(request)
    if not principal or via not in ("bearer", "basic", "x-api-key") or principal != request.get("client"):
        raise memory.MemoryUnauthenticated
    if request.path.endswith(("/version", "/v1/version")):
        # Versionless on purpose: Hermes must not assume append support. This is
        # an adapter, not an advertised version of the full Hindsight server.
        return web.json_response({"gateway": "skirnir", "capabilities": ["recall", "bounded-retain", "operation-status"]})
    if "operation_id" in request.match_info:
        if service.outbox is None:
            return session_api.fail(404, "durable operation tracking is disabled")
        operation = UUID(request.match_info["operation_id"])
        row = await service.outbox.outbox_get(principal, operation)
        scope = memory.scope_for_principal(principal, row["project"], explicit=True)
        if service.bank(scope) != row["scope"].get("__bank"):
            raise SessionNotFound
        if "alias" in request.match_info and request.match_info["alias"] != row["project"]:
            raise SessionNotFound
        if request.method == "DELETE":
            if not scope.write:
                raise memory.MemoryDenied
            await service.outbox.outbox_cancel(principal, operation)
            return web.json_response({"operation_id": str(operation), "state": "cancelled", "upstream_deleted": False})
        if request.method == "POST" and request.path.endswith("/retry"):
            data = await session_api.body(request)
            session_api.fields(data, ("retry_id",), ("retry_id",))
            retry_id = session_api.text(data["retry_id"], "retry_id")
            deleting = row.get("delete_requested", False)
            if deleting:
                if not scope.delete:
                    raise memory.MemoryDenied
            else:
                service.guard(scope, write=True)
                if not service.cfg.retention_enabled or (row.get("payload") and memory.rejected_content(json.dumps(row["payload"]), service.cfg)):
                    raise memory.MemoryDenied
            from dataclasses import asdict
            result = await service.outbox.outbox_retry(principal, operation, retry_id,
                {**asdict(scope), "__bank": service.bank(scope)}, service.cfg.outbox_capacity, service.cfg.outbox_client_capacity)
            service.wakeup.set()
            return web.json_response({"operation_id": str(operation), "state": result["state"], "durable": True}, status=202)
        if request.method == "POST":
            if not scope.delete:
                raise memory.MemoryDenied
            from dataclasses import asdict
            await service.outbox.outbox_delete(principal, operation, {**asdict(scope), "__bank": service.bank(scope)})
            service.wakeup.set()
            state_value = (await service.outbox.outbox_get(principal, operation))["state"]
            return web.json_response({"operation_id": str(operation), "state": state_value, "durable": True}, status=202 if state_value != "deleted" else 200)
        if not (scope.read or scope.write or scope.delete):
            raise memory.MemoryDenied
        result = {k: row[k] for k in ("operation_id", "state", "attempts", "created_at", "updated_at")}
        if row["state"] == "accepted":
            upstream = await service.client.operation(service.bank(scope), str(operation), authorize=lambda: service.guard(scope))
            result["upstream_status"] = upstream.get("status", "unknown")
        for field in ("last_error", "retry_rounds"):
            result[field] = row.get(field)
        result["action"] = "delete" if row.get("delete_requested") else "retain"
        if gateway and result.get("upstream_status") == "not_found":
            return session_api.fail(404, "upstream operation no longer available")
        if gateway:
            # Hermes waits for actual upstream completion, never local 202 alone.
            result["status"] = result.get("upstream_status") or {"pending": "pending", "processing": "processing", "dead": "failed",
                "revoked": "cancelled", "cancelled": "cancelled", "deleted": "completed", "delete_pending": "pending"}.get(row["state"], "pending")
        return web.json_response(result)
    alias = request.match_info["alias"]
    recall = request.path.endswith("/recall")
    scope = memory.scope_for(request, alias, read=recall, write=not recall, explicit=True)
    data = await session_api.body(request)
    if recall:
        session_api.fields(data, ("query", "budget", "max_tokens", "types", "tags", "tags_match", "tag_groups", "min_scores", "include", "trace", "query_timestamp", "prefer_observations", "temporal_window"), ("query",))
        # The gateway forwards no expansion knobs. Explicitly reject filters whose
        # semantics would be silently lost; selected provider defaults are supported.
        include = data.get("include")
        if include is not None:
            session_api.fields(include, ("entities", "chunks", "source_facts"))
            if any(value is not None for value in include.values()):
                raise ValueError("unsupported gateway expansion")
        if any(data.get(k) for k in ("tags", "tag_groups", "min_scores", "query_timestamp", "trace", "prefer_observations", "temporal_window")):
            raise ValueError("unsupported gateway recall options")
        if data.get("budget", "low") not in ("low", "mid", "high") or data.get("tags_match", "any") != "any":
            raise ValueError("unsupported gateway recall budget or tag match")
        if data.get("types") not in (None, ["observation"], ["world", "experience", "observation"]):
            raise ValueError("unsupported recall fact types")
        tokens = data.get("max_tokens", service.cfg.recall_max_tokens)
        if type(tokens) is not int or not 1 <= tokens <= 100000:
            raise ValueError("invalid recall budget")
        items = await service.recall(scope, data["query"], types=data.get("types"), byte_budget=tokens)
        return web.json_response({"results": [{"id": x["source"], "text": x["text"], "metadata": x.get("provenance", {})} for x in items],
                                  "trusted": False})
    if service.outbox is None:
        return session_api.fail(503, "native retain gateway requires the persistent outbox")
    session_api.fields(data, ("items", "async", "operation_id", "document_tags"), ("items",))
    if data.get("async") is not True or data.get("document_tags"):
        raise ValueError("gateway requires async retain without document tag mutation")
    items = data["items"]
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("gateway accepts one bounded text item per operation")
    item = items[0]
    session_api.fields(item, ("content", "document_id", "context", "metadata", "timestamp", "tags", "update_mode", "resolve_entities", "entities", "observation_scopes", "strategy"), ("content",))
    if any(item.get(k) is not None for k in ("entities", "strategy")) or item.get("resolve_entities", True) is not True or item.get("tags"):
        raise ValueError("unsupported gateway retain extraction options")
    if item.get("observation_scopes") not in (None, "shared"):
        raise ValueError("unsupported gateway observation scope")
    if item.get("update_mode") not in (None, "replace"):
        raise ValueError("append is not supported by this gateway")
    # Untrusted provider metadata cannot promote transcript/assistant output to truth.
    origin = provenance({}, principal, confirmed=False)
    doc = item.get("document_id")
    if doc is not None:
        session_api.text(doc, "document_id", 256)
        origin["source_refs"] = json.dumps(["provider-document:" + hashlib.sha256(doc.encode()).hexdigest()])
    content = item["content"]
    # Identical replays/compaction retains share one immutable operation. Changed
    # versions remain distinct evidence, not destructive edits of shared facts.
    event = str(UUID(data["operation_id"])) if data.get("operation_id") else hashlib.sha256(
        json.dumps([doc, content], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    operation = await service.submit(scope, event, content, confirmed=False, provenance=origin, shared_observations=item.get("observation_scopes") == "shared")
    # Official SDKs deserialize RetainResponse only for HTTP 200. async=true
    # still means admission, not completed extraction; our REST API uses 202.
    return web.json_response({"success": True, "bank_id": alias, "items_count": 1, "operation_id": operation,
                              "async": True, "durable": True})
