"""Quiet for a measurement, per node (Router 0.21.0, ADR-0061).

A test bench that loads models on a node directly through Ollama looks like foreign memory to the router: the node turns
busy, the busy safety net unloads the measured model, and once the node is free again prewarm and the residency rule load
the rank-1 model back. Until `quiet_until` the router does none of that on this one node:

- the busy safety net (`poll.busy_unload_check`, the unload on the busy transition) unloads nothing,
- `poll.prewarm` and `poll.residency_check` are skipped, each with a log line naming the reason,
- busy detection stays on, so real traffic keeps away while the measured model holds the card.

The quiet ends by itself when `until` passes (`expire`, every tick) or on `DELETE /admin/nodes/{fp}/quiet`. The end
refuses while the node is busy and the safety net would then unload models, unless the caller accepts that
(`accept_unload: true`): ending the quiet too early was the trap of the global-settings workaround this replaces.
The state lives in memory only; a router restart ends every quiet.
"""
import math
import time

from aiohttp import web

from . import state
from .common import log, read_json
from .outcome import Problem, conflict, failed, invalid, not_found

MAX_QUIET_S = 12 * 3600   # a forgotten quiet ends within a working day at the latest
REASON_MAX = 200


def target(fp):
    """The active node behind an approved fingerprint, or a Problem."""
    e = state.REG.nodes.get(fp) if state.REG is not None else None
    if e is None:
        return not_found("unknown fingerprint")
    node = state.NODES.get(e.get("name"))
    if e.get("state") != "approved" or node is None or node.fp != fp:
        return conflict("node not approved or not active")
    return node


def parse_start(body, now):
    """(until, reason) from the request body, or a Problem."""
    until, reason = body.get("until"), body.get("reason")
    if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
        return invalid("until: epoch seconds expected")
    if until <= now:
        return invalid("until lies in the past")
    if until - now > MAX_QUIET_S:
        return invalid(f"until lies more than {MAX_QUIET_S // 3600} h ahead")
    if not isinstance(reason, str):
        return invalid("reason: text expected")
    reason = " ".join("".join(ch if ch.isprintable() else " " for ch in reason).split())[:REASON_MAX]
    if not reason:
        return invalid("reason must not be empty")
    return float(until), reason


def start(node, until, reason, now):
    """Begin or extend the quiet. Extending keeps `since`, so the journal window covers the whole measurement."""
    extended = node.quiet(now)
    if not extended:
        node.quiet_since = now
    node.quiet_until, node.quiet_reason = until, reason
    log.info("node %s quiet %s until %s (%s)", node.name, "extended" if extended else "begins",
             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)), reason)
    state.remember({"event": "quiet", "node": node.name, "until": until, "reason": reason, "extended": extended})
    state.MQTT_DIRTY.append(True)


def unload_after_end(node):
    """What the busy safety net would unload on this node once the quiet ends (empty if nothing)."""
    from . import poll   # late: poll imports this module
    if node.state != "busy" or not state.CFG.unload_on_busy:
        return []
    return poll.busy_unloadable(node)


def end(node, now, why):
    """End the quiet: log, journal, and prewarm once if the node is free (no transition to free would do it later).
    Returns what happens next as facts."""
    from . import poll   # late: poll imports this module
    reason = node.quiet_reason
    node.quiet_until, node.quiet_since, node.quiet_reason = 0.0, 0.0, ""
    will_unload = unload_after_end(node)
    prewarm = node.state == "free" and state.CFG.prewarm_on_free
    log.info("node %s quiet ends (%s, was: %s), state %s%s", node.name, why, reason, node.state,
             f", safety net will unload {', '.join(will_unload)}" if will_unload else "")
    state.remember({"event": "quiet_end", "node": node.name, "why": why, "reason": reason, "will_unload": will_unload})
    state.MQTT_DIRTY.append(True)
    if prewarm:
        state.spawn(poll.prewarm(node, 0, "quiet_end", since=node.free_at))
    return {"state": node.state, "will_unload": will_unload, "prewarm": prewarm}


def expire(node, now):
    """Called every tick: a quiet whose `until` passed ends by itself."""
    if node.quiet_until and not node.quiet(now):
        end(node, now, "expired")


def problem_response(p: Problem):
    return web.json_response({"error": p.message}, status=p.status)


async def handle_start(request):
    """POST /admin/nodes/{fp}/quiet {"until": <epoch s>, "reason": "..."}: quiet for this node until `until`."""
    node = target(request.match_info["fp"])
    if failed(node):
        return problem_response(node)
    body = (await read_json(request) if request.can_read_body else None)
    if not isinstance(body, dict):
        return problem_response(invalid("object expected"))
    now = time.time()
    parsed = parse_start(body, now)
    if failed(parsed):
        return problem_response(parsed)
    start(node, *parsed, now)
    return web.json_response({"ok": True, "node": node.name, "quiet": node.quiet_view(now)})


async def handle_end(request):
    """DELETE /admin/nodes/{fp}/quiet [{"accept_unload": true}]: end the quiet now. Refuses with 409 and the facts while
    the node is busy and the safety net would then unload models, unless the caller accepts that."""
    node = target(request.match_info["fp"])
    if failed(node):
        return problem_response(node)
    body = (await read_json(request) if request.can_read_body else None) or {}
    if not isinstance(body, dict) or not isinstance(body.get("accept_unload", False), bool):
        return problem_response(invalid("object with optional accept_unload: true|false expected"))
    now = time.time()
    if not node.quiet(now):
        return web.json_response({"ok": True, "node": node.name, "was_quiet": False, "quiet": None})
    will_unload = unload_after_end(node)
    if will_unload and not body.get("accept_unload", False):
        return web.json_response({
            "error": "node is busy and the router would unload models once the quiet ends",
            "node": node.name, "state": node.state, "busy_reason": node.busy_reason, "would_unload": will_unload,
            "quiet": node.quiet_view(now),
            "next": "unload the measured model yourself, or repeat with accept_unload: true"}, status=409)
    then = end(node, now, "lifted")
    return web.json_response({"ok": True, "node": node.name, "was_quiet": True, "quiet": None, "then": then})
