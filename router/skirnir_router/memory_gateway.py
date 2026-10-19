"""Allowlisted Hindsight recall/retain compatibility gateway and operation control.

The path bank is a configured project alias, not an upstream identifier. No arbitrary
proxying, reflect, bank administration, payload/status disclosure or tenant selection.
Functions return their result or a Problem (outcome.py).
"""
import asyncio
import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from aiohttp import web

from . import auth, memory, session_api
from .memory_outbox import upstream_id
from .outcome import Problem, bad_gateway, denied, failed, invalid, unavailable
from .reliability_store import OPERATION_NOT_FOUND

GATEWAY = "/v1/skirnir/hindsight"
SCOPE_DENIED = denied("memory scope denied")
# Client-facing wording per status.
PUBLIC = {400: "invalid memory gateway request", 403: SCOPE_DENIED.message, 404: OPERATION_NOT_FOUND.message,
          409: "memory operation conflicts or may already be processing upstream", 413: "memory request too large",
          503: "memory backend unavailable"}


def provenance(raw, principal, *, confirmed):
    """Server-side provenance record, or a Problem."""
    if failed(problem := session_api.fields(raw, ("source_refs", "kind", "version", "supersedes"))):
        return problem
    refs = raw.get("source_refs", [])
    if not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(x, str) or len(x) > 256 for x in refs):
        return invalid("invalid provenance references")
    kind = raw.get("kind", "confirmed_information" if confirmed else "assistant_derivation")
    if kind not in ("user_assertion", "confirmed_information", "assistant_derivation") or (not confirmed and kind == "confirmed_information"):
        return invalid("invalid knowledge kind")
    result = {"kind": kind, "origin_client": principal, "source_refs": json.dumps(refs), "confirmed": str(confirmed).lower()}
    # Wall-clock time is added on dispatch; it is not part of an event fingerprint.
    for key in ("version", "supersedes"):
        if key in raw:
            if failed(value := session_api.text(raw[key], key)):
                return value
            result[key] = value
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
    response = await dispatch(request)
    if isinstance(response, Problem):
        response = (auth._deny(request, 401, "memory credentials required") if response.status == 401
                    else memory.public_problem(response, PUBLIC))
    response.headers["Cache-Control"] = "no-store"
    return response


async def dispatch(request):
    service = request.app.get(memory.SERVICE_KEY)
    gateway = request.path.startswith(GATEWAY + "/")
    if service is None or not service.active() or (gateway and not service.cfg.gateway_enabled):
        return session_api.fail(404, "memory gateway disabled")
    principal, via = auth.resolve_client(request)
    if not principal or via not in ("bearer", "basic", "x-api-key") or principal != request.get("client"):
        return memory.UNAUTHENTICATED
    if request.path.endswith(("/version", "/v1/version")):
        # Versionless on purpose: Hermes must not assume append support. This is
        # an adapter, not an advertised version of the full Hindsight server.
        return web.json_response({"gateway": "skirnir", "capabilities": ["recall", "bounded-retain", "operation-status"]})
    if "operation_id" in request.match_info:
        return await operation_control(request, service, principal, gateway)
    alias = request.match_info["alias"]
    recall = request.path.endswith("/recall")
    scope = memory.scope_for(request, alias, read=recall, write=not recall, explicit=True)
    if failed(scope):
        return scope
    data = await session_api.body(request)
    if failed(data):
        return data
    if recall:
        return await gateway_recall(service, scope, data)
    return await gateway_retain(service, scope, principal, alias, data)


async def operation_control(request, service, principal, gateway):
    if service.outbox is None:
        return session_api.fail(404, "durable operation tracking is disabled")
    operation = session_api.uuid(request.match_info["operation_id"])
    if operation is None:
        return invalid("invalid operation id")
    row = await service.outbox.outbox_get(principal, operation)
    if failed(row):
        return row
    scope = memory.scope_for_principal(principal, row["project"], explicit=True)
    if failed(scope):
        return scope
    if service.bank(scope) != row["scope"].get("__bank"):
        return OPERATION_NOT_FOUND
    if "alias" in request.match_info and request.match_info["alias"] != row["project"]:
        return OPERATION_NOT_FOUND
    if request.method == "DELETE":
        if not scope.write:
            return SCOPE_DENIED
        cancelled = await service.outbox.outbox_cancel(principal, operation)
        if failed(cancelled):
            return cancelled
        return web.json_response({"operation_id": str(operation), "state": "cancelled", "upstream_deleted": False})
    if request.method == "POST" and request.path.endswith("/retry"):
        data = await session_api.body(request)
        if failed(data):
            return data
        if failed(problem := session_api.fields(data, ("retry_id",), ("retry_id",))):
            return problem
        retry_id = session_api.text(data["retry_id"], "retry_id")
        if failed(retry_id):
            return retry_id
        if row.get("delete_requested", False):
            if not scope.delete:
                return SCOPE_DENIED
        elif (not service.current(scope, write=True) or not service.cfg.retention_enabled
              or (row.get("payload") and memory.rejected_content(json.dumps(row["payload"]), service.cfg,
                                                                 private_addresses=scope.private_addresses))):
            return SCOPE_DENIED
        result = await service.outbox.outbox_retry(principal, operation, retry_id,
            {**asdict(scope), "__bank": service.bank(scope)}, service.cfg.outbox_capacity, service.cfg.outbox_client_capacity)
        if failed(result):
            return result
        service.wakeup.set()
        return web.json_response({"operation_id": str(operation), "state": result["state"], "durable": True}, status=202)
    if request.method == "POST":
        if not scope.delete:
            return SCOPE_DENIED
        deleted = await service.outbox.outbox_delete(principal, operation, {**asdict(scope), "__bank": service.bank(scope)})
        if failed(deleted):
            return deleted
        service.wakeup.set()
        current = await service.outbox.outbox_get(principal, operation)
        if failed(current):
            return current
        return web.json_response({"operation_id": str(operation), "state": current["state"], "durable": True},
                                 status=202 if current["state"] != "deleted" else 200)
    if not (scope.read or scope.write or scope.delete):
        return SCOPE_DENIED
    result = {k: row[k] for k in ("operation_id", "state", "attempts", "created_at", "updated_at")}
    if row["state"] == "accepted":   # acknowledged, not yet verified by the outbox: show the live upstream state
        upstream = await service.client.operation(service.bank(scope), upstream_id(operation, row), authorize=lambda: service.current(scope))
        if failed(upstream):
            return SCOPE_DENIED if upstream.status == 403 else upstream
        result["upstream_status"] = upstream.get("status", "unknown")
    for field in ("last_error", "retry_rounds", "upstream_attempts"):
        result[field] = row.get(field)
    result["action"] = "delete" if row.get("delete_requested") else "retain"
    if gateway and result.get("upstream_status") == "not_found":
        return session_api.fail(404, "upstream operation no longer available")
    if gateway:
        # Hermes waits for verified completion, never local 202 alone. The outbox's state wins over a single upstream
        # reading: an accepted job whose extraction failed upstream is resubmitted, so it is still in progress.
        mapped = {"pending": "pending", "processing": "processing", "accepted": "processing", "completed": "completed",
                  "dead": "failed", "revoked": "cancelled", "cancelled": "cancelled", "deleted": "completed",
                  "delete_pending": "pending"}.get(row["state"], "pending")
        live = result.get("upstream_status")
        result["status"] = live if live in ("pending", "processing", "completed") else mapped
    return web.json_response(result)


async def gateway_recall(service, scope, data):
    names = ("query", "budget", "max_tokens", "types", "tags", "tags_match", "tag_groups", "min_scores", "include", "trace",
             "query_timestamp", "prefer_observations", "temporal_window")
    if failed(problem := session_api.fields(data, names, ("query",))):
        return problem
    # The gateway forwards no expansion knobs. Explicitly reject filters whose
    # semantics would be silently lost; selected provider defaults are supported.
    include = data.get("include")
    if include is not None:
        if failed(problem := session_api.fields(include, ("entities", "chunks", "source_facts"))):
            return problem
        if any(value is not None for value in include.values()):
            return invalid("unsupported gateway expansion")
    if any(data.get(k) for k in ("tags", "tag_groups", "min_scores", "query_timestamp", "trace", "prefer_observations", "temporal_window")):
        return invalid("unsupported gateway recall options")
    if data.get("budget", "low") not in ("low", "mid", "high") or data.get("tags_match", "any") != "any":
        return invalid("unsupported gateway recall budget or tag match")
    if data.get("types") not in (None, ["observation"], ["world", "experience", "observation"]):
        return invalid("unsupported recall fact types")
    # A client's max_tokens counts fact text, as in Hindsight, and can only lower what the server passes on; the
    # server cap recall_max_bytes still bounds the whole block. It never changes Hindsight's own selection budget.
    tokens = data.get("max_tokens")
    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= 100000):
        return invalid("invalid recall budget")
    recalled = await service.recall(scope, data["query"], types=data.get("types"), text_budget=tokens)
    if failed(recalled):
        return recalled
    return memory.recall_response({"results": [{"id": x["source"], "text": x["text"], "metadata": x.get("provenance", {})}
                                               for x in recalled.items], "trusted": False}, recalled)


async def gateway_retain(service, scope, principal, alias, data):
    if service.outbox is None:
        return session_api.fail(503, "native retain gateway requires the persistent outbox")
    if failed(problem := session_api.fields(data, ("items", "async", "operation_id", "document_tags"), ("items",))):
        return problem
    if data.get("async", False) not in (True, False) or data.get("document_tags"):
        return invalid("gateway retain needs a boolean async flag and no document tag mutation")
    # Synchronous retain (the Hindsight default): admitted durably like async, then answered once the outbox verified
    # the extraction, bounded by sync_wait_s. The waiting slots are limited so clients cannot pin connections.
    sync = data.get("async", False) is False
    if sync and service.sync_waiting >= service.cfg.sync_max_waiting:
        return unavailable("too many synchronous retains waiting; retry later or retain asynchronously")
    items = data["items"]
    if not isinstance(items, list) or len(items) != 1:
        return invalid("gateway accepts one bounded text item per operation")
    item = items[0]
    names = ("content", "document_id", "context", "metadata", "timestamp", "tags", "update_mode", "resolve_entities", "entities",
             "observation_scopes", "strategy")
    if failed(problem := session_api.fields(item, names, ("content",))):
        return problem
    refused = [k for k, unsupported in (("entities", item.get("entities") is not None), ("strategy", item.get("strategy") is not None),
                                        ("resolve_entities", item.get("resolve_entities", True) is not True), ("tags", bool(item.get("tags"))))
               if unsupported]
    if refused:   # named, so the client sees which option to drop (error.reason) instead of a generic 400
        return invalid("unsupported gateway retain option: " + refused[0])
    if item.get("observation_scopes") not in (None, "shared"):
        return invalid("unsupported gateway observation scope")
    if item.get("update_mode") not in (None, "replace"):
        return invalid("append is not supported by this gateway")
    # Untrusted provider metadata cannot promote transcript/assistant output to truth.
    origin = provenance({}, principal, confirmed=False)
    doc = item.get("document_id")
    if doc is not None:
        if failed(checked := session_api.text(doc, "document_id", 256)):
            return checked
        origin["source_refs"] = json.dumps(["provider-document:" + hashlib.sha256(doc.encode()).hexdigest()])
    content = item["content"]
    # Identical replays/compaction retains share one immutable operation. Changed
    # versions remain distinct evidence, not destructive edits of shared facts.
    if data.get("operation_id"):
        given = session_api.uuid(data["operation_id"])
        if given is None:
            return invalid("invalid operation id")
        event = str(given)
    else:
        event = hashlib.sha256(json.dumps([doc, content], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    operation = await service.submit(scope, event, content, confirmed=False, provenance=origin, shared_observations=item.get("observation_scopes") == "shared")
    if failed(operation):
        return operation
    if sync:
        verified = await wait_verified(service, principal, operation)
        if failed(verified):
            return verified
    # Official SDKs deserialize RetainResponse only for HTTP 200. async=true still means admission, not completed
    # extraction (our REST API uses 202); async=false answers 200 only after verified completion.
    return web.json_response({"success": True, "bank_id": alias, "items_count": 1, "operation_id": operation,
                              "async": not sync, "durable": True})


async def wait_verified(service, principal, operation):
    """None once the outbox verified the extraction as completed, or a Problem. The job stays durable either way:
    on timeout it keeps going, and resending identical content returns the same operation."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + service.cfg.sync_wait_s
    service.sync_waiting += 1
    try:   # no except: only keeps the waiting counter balanced, also when the client disconnects
        while True:
            service.wakeup.set()
            row = await service.outbox.outbox_get(principal, UUID(operation))
            if failed(row):
                return row
            if row["state"] == "completed":
                return None
            if row["state"] in ("dead", "revoked", "cancelled", "deleted"):
                return bad_gateway(f"memory extraction did not complete (state {row['state']}, category {row.get('last_error') or 'none'}); "
                                   f"operation_id={operation}")
            if loop.time() >= deadline:
                return Problem(504, f"retain accepted durably but not yet confirmed within {service.cfg.sync_wait_s} s; operation_id={operation}; "
                                    "resending identical content is safe")
            await asyncio.sleep(0.5)
    finally:
        service.sync_waiting -= 1
