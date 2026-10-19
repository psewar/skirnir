"""Opt-in managed turns inside the existing inference pipeline, never a second router.

Functions return their result or a Problem (outcome.py); the relay answers a Problem
before or inside the stream.
"""
import asyncio
import copy
import hashlib
from uuid import uuid4

from . import auth, cloud, metrics, session_api, state
from .context_config import ContextConfig
from .context_manager import compose, encoded, estimate, persistable, project
from .outcome import Problem, bad_gateway, denied, failed, invalid, not_found, too_large, unauthenticated, unavailable
from .session_types import NOT_FOUND

KEY = "skirnir_managed_turn"
MANAGED_UNAVAILABLE = unavailable("managed session unavailable; inspect history before retrying the same event_id")


def stored(result):
    """A store result for a managed turn: an unavailable database is reported with managed retry guidance."""
    return MANAGED_UNAVAILABLE if failed(result) and result.status == 503 else result


def settings():
    return getattr(state.CFG, "context_manager", ContextConfig())


def parse(raw):
    """routing.session as {id, event_id}, None when absent, or a Problem."""
    if raw is None:
        return None
    if failed(problem := session_api.fields(raw, ("id", "mode", "event_id"), ("id", "mode", "event_id"))):
        return problem
    if raw["mode"] != "skirnir-managed":
        return invalid("routing.session.mode must be skirnir-managed")
    sid = session_api.uuid(raw["id"])
    if sid is None:
        return invalid("routing.session.id must be a UUID")
    event_id = session_api.text(raw["event_id"], "event_id")
    return event_id if failed(event_id) else {"id": sid, "event_id": event_id}


def validate_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        return invalid("managed requests require 1..64 new messages only")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "tool", "function"):
            return invalid("managed input must contain new instruction/user/tool messages, not assistant history")
        if message.get("images") or not isinstance(message.get("content"), str) or message.get("tool_calls"):
            return invalid("managed sessions currently require text content and completed tool results")
    if messages[-1]["role"] not in ("user", "tool", "function"):
        return invalid("managed delta must end in user or tool input")
    return None


def principal(request):
    owner, via = auth.resolve_client(request)
    if owner is None or via not in ("bearer", "basic", "x-api-key") or owner != request.get("client"):
        return unauthenticated("managed sessions require authenticated client credentials")
    return owner


async def begin(request, path, body, req):
    """Claim the managed turn and project its context into body. None, or a Problem."""
    if not req.session:
        return None
    if not settings().enabled or not getattr(state.CFG, "session_store", None) or not state.CFG.session_store.enabled:
        return not_found("managed sessions disabled")
    if path != "/api/chat" or any(body.get(k) for k in ("system", "prompt", "suffix", "raw", "images", "template", "context")):
        return invalid("managed sessions require chat messages without template overrides")
    # No implicit propagation of stored history to remote inference. A later explicit
    # cloud grant can expand this boundary; ordinary/stateless cloud is unchanged.
    if req.execution == "cloud":
        return denied("managed session inference is local only")
    if failed(problem := validate_messages(body.get("messages"))):
        return problem
    owner = principal(request)
    if failed(owner):
        return owner
    store = request.app.get(session_api.STORE_KEY)
    if store is None:
        return MANAGED_UNAVAILABLE
    sid = req.session["id"]
    session = stored(await store.get(owner, sid))
    if failed(session):
        return session
    if not session_api.allowed_project(owner, session["project"]):
        return NOT_FOUND
    if req.memory and req.memory["project"] != session["project"]:
        return denied("session and memory projects must match")
    client = auth.client_auth_cfg()["clients"][owner]
    requested = max((req.data_class or state.CFG.cloud["default_data_class"],
                     client.get("data_class") or state.CFG.cloud["default_data_class"]), key=cloud.class_index)
    if cloud.class_index(requested) > cloud.class_index(session["data_class"]):
        return denied("input data class exceeds immutable session classification")
    req.data_class, req.execution = session["data_class"], "local"
    cfg = settings()
    # Input identity includes inference-affecting options, excluding transport and
    # model selection: retries/model switches must not duplicate persisted input.
    fingerprint = hashlib.sha256(encoded({k: v for k, v in body.items() if k not in ("model", "stream", "keep_alive")})).hexdigest()
    claim = stored(await store.claim(owner, sid, req.session["event_id"], fingerprint, copy.deepcopy(body["messages"]), cfg))
    if failed(claim):
        return claim
    turn = Turn(request, req, store, owner, sid, claim["lease"], cfg)
    request[KEY] = turn
    turn.heartbeat = asyncio.create_task(turn.renew())
    context = stored(await store.context(owner, sid, cfg))
    if failed(context):
        return context
    turn.context = context
    body["messages"] = compose(turn.context)
    req.require |= {"tools"} if any(m.get("tool_calls") or m.get("role") in ("tool", "function") for m in body["messages"]) else set()
    return None


class Turn:
    def __init__(self, request, req, store, owner, sid, lease, cfg):
        self.request, self.req, self.store, self.owner, self.sid, self.lease, self.cfg = request, req, store, owner, sid, lease, cfg
        self.heartbeat = None
        self.context = None
        self.finished = False
        self.lost = False

    def check(self):
        """None while the turn may continue, or a Problem when lease or policy changed."""
        if self.lost:
            return Problem(409, "managed turn lease lost")
        if not settings().enabled or not state.CFG.session_store.enabled:
            return unavailable("managed session configuration changed")
        if principal(self.request) != self.owner or not session_api.allowed_project(self.owner, self.context["session"]["project"]):
            return denied("session policy changed")
        client = auth.client_auth_cfg()["clients"][self.owner]
        if cloud.class_index(self.req.data_class) > cloud.class_index(self.context["session"]["data_class"]):
            return denied("memory/request data class exceeds session classification")
        if cloud.class_index(client.get("data_class") or state.CFG.cloud["default_data_class"]) > cloud.class_index(self.context["session"]["data_class"]):
            return denied("session data policy changed")
        return None

    async def renew(self):
        while True:
            await asyncio.sleep(self.cfg.lease_s / 3)
            if failed(await self.store.renew(self.owner, self.sid, self.lease, self.cfg.lease_s)):
                self.lost = True
                metrics._counter("skirnir_context_events_total", {"operation": "lease", "outcome": "lost"})
                return

    async def target(self, body, tier, ctx):
        """Fit the projected context to this tier. None, or a Problem."""
        if failed(problem := self.check()):
            return problem
        if tier.get("cloud"):
            return denied("managed session inference is local only")
        ctx = min(ctx or tier["num_ctx"], tier["num_ctx"])
        model = state.CFG.catalog_twin(tier["model"]) or {}
        if model.get("context_tokens"):
            ctx = min(ctx, int(model["context_tokens"]))
        opts = dict(body.get("options") or {})
        output = opts.get("num_predict", self.cfg.reserve_tokens)
        if type(output) is not int:
            return invalid("invalid managed generation options")
        if output <= 0:
            output = self.cfg.reserve_tokens
        budget = ctx - output - self.cfg.safety_tokens
        if budget <= 0:
            return too_large("output and safety reserves exceed target context")
        from . import memory
        ephemeral = self.req.memory_context
        items = list(self.req.memory_items)
        if self.cfg.optimize_recall and ephemeral:
            while items and len(memory.packed(items).encode()) > budget * self.cfg.recall_budget_percent // 100:
                items.pop()
            ephemeral = {"role": "user", "content": memory.packed(items)} if items else None
        before = estimate(body, compose(self.context, ephemeral))
        projected = project(self.context, body, budget, self.cfg, tier["model"], ephemeral, state.CFG.limits["max_messages"])
        # Too large: drop recalled facts one by one (lowest priority last) while optimize_recall allows it.
        while failed(projected) and self.cfg.optimize_recall and items:
            items.pop()
            ephemeral = {"role": "user", "content": memory.packed(items)} if items else None
            projected = project(self.context, body, budget, self.cfg, tier["model"], ephemeral, state.CFG.limits["max_messages"])
        if failed(projected):
            return projected
        messages, compacted = projected
        used = estimate(body, messages)
        for part, value in (("prompt_budget", budget), ("estimated_prompt", used), ("output_reserve", output),
                            ("recall", len(ephemeral["content"].encode()) if ephemeral else 0)):
            metrics._hist("skirnir_context_tokens", {"part": part}, (256, 1024, 4096, 16384, 65536, 262144), value)
        if self.req.memory_scope is not None:
            scope = self.req.memory_scope
            remembered = await self.store.remember_memory_scope(self.owner, self.sid, self.lease,
                {"project": scope.project, "shared_scope": scope.shared_scope, "data_class": scope.data_class})
            if failed(remembered):
                return stored(remembered)
        if compacted:
            metrics._hist("skirnir_compaction_saved_tokens", {}, (0, 256, 1024, 4096, 16384, 65536), max(0, before - used))
            version = await self.store.compact(self.owner, self.sid, self.lease, compacted["through_seq"], compacted["payload"])
            if failed(version):
                return stored(version)
            self.context["compaction"] = version
            self.context["messages"] = [row for row in self.context["messages"] if row["seq"] > version["through_seq"]]
            metrics._counter("skirnir_context_events_total", {"operation": "compaction", "outcome": "ok"})
        opts.update(num_predict=output, num_ctx=ctx)
        body.update(messages=messages, options=opts)
        return None

    async def commit(self, capture):
        """Persist the completed assistant message. None, or a Problem."""
        if self.finished:
            return None
        if failed(problem := self.check()):
            return problem
        if not capture.done:
            return bad_gateway("incomplete managed response; raw input retained")
        transport = self.request.transport
        if transport is None or transport.is_closing():
            return bad_gateway("managed response interrupted; raw input retained")
        checkpoint = None
        if self.cfg.auto_checkpoints:
            previous = self.context.get("checkpoint")
            checkpoint = dict(previous["payload"]) if previous else {"schema_version": 1, "goals": [], "open_steps": [], "artifacts": []}
            checkpoint["last_result"] = "Assistant tool calls observed; results unverified" if capture.message.get("tool_calls") else "Assistant response completed; external work unverified"
            marker = "[Skirnir] Resolve outstanding tool calls; results are unverified"
            steps = list(checkpoint.get("open_steps", []))
            if (checkpoint.get("provenance") or {}).get("source") == "skirnir-observed":
                steps = [s for s in steps if s != marker]
            if capture.message.get("tool_calls") and marker not in steps:
                steps.append(marker)
            checkpoint["open_steps"] = steps
            checkpoint["provenance"] = {"source": "skirnir-observed", "resumable_execution": False}
            from .memory import rejected_content, settings as memory_settings
            if rejected_content(encoded(checkpoint).decode(), memory_settings()):
                checkpoint = None
        if checkpoint is None:
            written = await self.store.finish_turn(self.owner, self.sid, self.lease, capture.message)
        else:
            written = await self.store.finish_turn(self.owner, self.sid, self.lease, capture.message, checkpoint=checkpoint)
        if failed(written):
            return stored(written)
        self.finished = True
        metrics._counter("skirnir_context_events_total", {"operation": "turn", "outcome": "completed"})
        return None

    async def close(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            await asyncio.gather(self.heartbeat, return_exceptions=True)
        if not self.finished:
            # A failed write needs no handling: the lease expires independently and raw input remains durable.
            await self.store.fail_turn(self.owner, self.sid, self.lease)
            metrics._counter("skirnir_context_events_total", {"operation": "turn", "outcome": "incomplete"})


class AssistantCapture:
    """Capture the canonical native response after tool rescue, before shape conversion."""
    def __init__(self, limit):
        self.limit, self.size, self.done = limit, 0, False
        self.message = {"role": "assistant", "content": ""}

    def observe(self, result):
        """Add one native response chunk. None, or a Problem (502) for anything that cannot be persisted."""
        if self.done or not isinstance(result, dict) or result.get("error"):
            return bad_gateway("invalid managed response")
        message = result.get("message") or {}
        if not isinstance(message, dict):
            return bad_gateway("invalid managed message")
        if not persistable(message):
            return bad_gateway("invalid managed JSON content")
        self.size += len(encoded(message))
        if self.size > self.limit:
            return bad_gateway("managed response exceeds persistence limit")
        for key in ("content", "thinking"):
            value = message.get(key, "")
            if not isinstance(value, str):
                return bad_gateway("invalid managed text")
            if value:
                self.message[key] = self.message.get(key, "") + value
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            return bad_gateway("invalid managed tool calls")
        for call in calls:
            # Native Ollama sends complete tool calls (arguments are objects).
            # Reject unsupported partial/fragmented calls instead of saving corrupt state.
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                return bad_gateway("invalid managed tool call")
            fn = call["function"]
            if not isinstance(fn.get("name"), str) or not fn["name"].strip() or not isinstance(fn.get("arguments", {}), dict):
                return bad_gateway("managed tool calls require complete native arguments")
            if not call.get("id"):
                call["id"] = "call-" + uuid4().hex
            if failed(session_api.text(call["id"], "tool call id")):
                return bad_gateway("invalid managed tool identifier")
            if any(old["id"] == call["id"] for old in self.message.get("tool_calls", [])):
                return bad_gateway("duplicate or fragmented managed tool call")
            call["index"] = len(self.message.get("tool_calls", []))
            self.message.setdefault("tool_calls", []).append(copy.deepcopy(call))
        self.done = result.get("done") is True
        return None


async def lines(content, limit):
    """Bound the complete managed NDJSON reply before buffering arbitrary lines. Yields lines (bytes);
    a Problem as the last item means the reply exceeded the limit."""
    buffer = bytearray()
    total = 0
    async for chunk in content.iter_chunked(65536):
        total += len(chunk)
        if total > limit:
            yield bad_gateway("managed response exceeds persistence limit")
            return
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            yield bytes(line) + b"\n"
    if buffer:
        yield bytes(buffer)
