"""Review of writes into a shared memory scope before they reach the bank (ADR-0145).

A shared scope with `review: true` holds a non-reviewer's write in the durable outbox as `awaiting_review`
(memory_outbox.submit, reliability_store.outbox_put). Its reviewers list the held writes and approve or reject them here.
Approval moves the job to `pending`, so the outbox worker delivers it; rejection drops the payload. Only a reviewer of the
scope's bank sees or decides a held write; for everyone else the operation does not exist here (404).
Functions return their result or a Problem (outcome.py).
"""
import json
from dataclasses import fields

from aiohttp import web

from . import auth, memory, session_api
from .common import log
from .outcome import denied, failed, invalid
from .reliability_store import OPERATION_NOT_FOUND

REVIEW_LIMIT = 50
REVIEWER_ONLY = denied("listing held writes needs a reviewer of the shared scope")
DISABLED = (404, "durable operation tracking is disabled")
COUNTED = {"approve": "approved", "reject": "rejected", "revoke": "revoked"}


async def handle_list(request):
    return await memory._api(request, lambda service: _list(request, service))


async def handle_decision(request):
    return await memory._api(request, lambda service: _decide(request, service))


async def _list(request, service):
    """One page of the writes held for review in the reviewer's shared scope, oldest first, with the exact text the
    backend would extract from. Entries the content filter rejects for this reviewer are listed by id only."""
    if service.outbox is None:
        return session_api.fail(*DISABLED)
    data = await session_api.body(request)
    if failed(data):
        return data
    if failed(problem := session_api.fields(data, ("project", "limit", "offset"), ("project",))):
        return problem
    project = session_api.text(data["project"], "project")
    if failed(project):
        return project
    scope = memory.scope_for(request, project, read=True, explicit=True)
    if failed(scope):
        return scope
    if scope not in memory.reviewer_scopes(scope.principal):
        return REVIEWER_ONLY
    limit, offset = data.get("limit", 20), data.get("offset", 0)
    if type(limit) is not int or not 1 <= limit <= REVIEW_LIMIT or type(offset) is not int or offset < 0:
        return invalid(f"review limit must be 1..{REVIEW_LIMIT} and offset at least 0")
    rows = await service.outbox.outbox_held(service.bank(scope), limit, offset)
    if failed(rows):
        return rows
    items, withheld = [], []
    for row in rows:
        payload = row.get("payload") or {}
        if memory.rejected_content(json.dumps(payload), service.cfg, private_addresses=scope.private_addresses):
            withheld.append({"operation_id": row["operation_id"]})
            continue
        items.append({"operation_id": row["operation_id"], "writer": row["owner"], "created_at": row["created_at"],
                      "expires_at": row["available_at"], "content": payload.get("content"), "context": payload.get("context"),
                      "confirmed": payload.get("confirmed"), "provenance": payload.get("provenance") or {}})
    return web.json_response({"project": project, "items": items, "withheld": withheld, "limit": limit, "offset": offset},
                             headers={"Cache-Control": "no-store"})


async def _decide(request, service):
    """Approve or reject one held write. Approval checks the writer's grant again: a writer that lost its right since the
    write turns the job into `revoked` instead of delivering it."""
    if service.outbox is None:
        return session_api.fail(*DISABLED)
    principal, via = auth.resolve_client(request)
    if not principal or via not in ("bearer", "basic", "x-api-key") or principal != request.get("client"):
        return memory.UNAUTHENTICATED
    operation = session_api.uuid(request.match_info["operation_id"])
    if operation is None:
        return invalid("invalid operation id")
    data = await session_api.body(request)
    if failed(data):
        return data
    if failed(problem := session_api.fields(data, ())):
        return problem
    row = await service.outbox.outbox_find(operation)
    if failed(row):
        return row
    bank = row["scope"].get("__bank")
    reviewer = next((s for s in memory.reviewer_scopes(principal) if service.bank(s) == bank), None)
    if reviewer is None:   # not a reviewer of this bank: the held write is not revealed, also not to its own writer
        return OPERATION_NOT_FOUND
    if row["state"] != "awaiting_review":   # already decided, expired or withdrawn: say which, as a fact
        return web.json_response({"operation_id": str(operation), "state": row["state"], "reason": "not_awaiting_review"},
                                 status=409, headers={"Cache-Control": "no-store"})
    decision = "approve" if request.path.endswith("/approve") else "reject"
    if decision == "approve":
        writer = memory.Scope(**{f.name: row["scope"][f.name] for f in fields(memory.Scope) if f.name in row["scope"]})
        if not (service.current(writer, write=True) and service.bank(writer) == bank):
            decision = "revoke"
    result = await service.outbox.outbox_review(operation, bank, decision, service.cfg.outbox_capacity,
                                                service.cfg.outbox_client_capacity)
    if failed(result):
        return result
    # Visible afterwards without content: who released or refused whose write into which shared scope.
    log.info("memory review: %s %s operation %s of %s in shared scope %s", principal, COUNTED[decision], operation,
             row["owner"], reviewer.shared_scope)
    memory.count("review", COUNTED[decision])
    if result["state"] == "pending":
        service.wakeup.set()
    answer = {"operation_id": str(operation), "state": result["state"]}
    if result.get("last_error"):
        answer["reason"] = result["last_error"]
    return web.json_response(answer, headers={"Cache-Control": "no-store"})


def register(app):
    app.router.add_post(memory.PREFIX + "/review", handle_list)
    app.router.add_post(memory.PREFIX + "/operations/{operation_id}/approve", handle_decision)
    app.router.add_post(memory.PREFIX + "/operations/{operation_id}/reject", handle_decision)
