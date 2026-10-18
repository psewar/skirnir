"""Opt-in managed turns inside the existing inference pipeline, never a second router."""
import asyncio
import copy
import hashlib
from uuid import UUID, uuid4

from . import auth, cloud, metrics, session_api, state
from .context_config import ContextConfig
from .context_manager import compose, encoded, project
from .session_store import SessionCapacity, SessionConflict, SessionNotFound, SessionUnavailable

KEY = "skirnir_managed_turn"
ERRORS = (SessionCapacity, SessionConflict, SessionNotFound, SessionUnavailable)


class ManagedError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def failure(exc):
    if isinstance(exc, SessionCapacity):
        return ManagedError(413, str(exc))
    if isinstance(exc, SessionNotFound):
        return ManagedError(404, "session not found")
    if isinstance(exc, SessionConflict):
        return ManagedError(409, str(exc))
    return ManagedError(503, "managed session unavailable; inspect history before retrying the same event_id")


def settings():
    return getattr(state.CFG, "context_manager", ContextConfig())


def parse(raw):
    if raw is None:
        return None
    session_api.fields(raw, ("id", "mode", "event_id"), ("id", "mode", "event_id"))
    if raw["mode"] != "skirnir-managed":
        raise ValueError("routing.session.mode must be skirnir-managed")
    try:
        sid = UUID(raw["id"])
    except (ValueError, TypeError, AttributeError):
        raise ValueError("routing.session.id must be a UUID") from None
    return {"id": sid, "event_id": session_api.text(raw["event_id"], "event_id")}


def validate_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise ValueError("managed requests require 1..64 new messages only")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer", "user", "tool", "function"):
            raise ValueError("managed input must contain new instruction/user/tool messages, not assistant history")
        if message.get("images") or not isinstance(message.get("content"), str) or message.get("tool_calls"):
            raise ValueError("managed sessions currently require text content and completed tool results")
    if messages[-1]["role"] not in ("user", "tool", "function"):
        raise ValueError("managed delta must end in user or tool input")


def principal(request):
    owner, via = auth.resolve_client(request)
    if owner is None or via not in ("bearer", "basic", "x-api-key") or owner != request.get("client"):
        raise ManagedError(401, "managed sessions require authenticated client credentials")
    return owner


async def begin(request, path, body, req):
    if not req.session:
        return
    if not settings().enabled or not getattr(state.CFG, "session_store", None) or not state.CFG.session_store.enabled:
        raise ManagedError(404, "managed sessions disabled")
    if path != "/api/chat" or any(body.get(k) for k in ("system", "prompt", "suffix", "raw", "images", "template", "context")):
        raise ValueError("managed sessions require chat messages without template overrides")
    # No implicit propagation of stored history to remote inference. A later explicit
    # cloud grant can expand this boundary; ordinary/stateless cloud is unchanged.
    if req.execution == "cloud":
        raise ManagedError(403, "managed session inference is local only")
    validate_messages(body.get("messages"))
    owner = principal(request)
    store = request.app.get(session_api.STORE_KEY)
    if store is None:
        raise SessionUnavailable
    sid = req.session["id"]
    session = await store.get(owner, sid)
    if not session_api.allowed_project(owner, session["project"]):
        raise SessionNotFound
    if req.memory and req.memory["project"] != session["project"]:
        raise ManagedError(403, "session and memory projects must match")
    client = auth.client_auth_cfg()["clients"][owner]
    requested = max((req.data_class or state.CFG.cloud["default_data_class"],
                     client.get("data_class") or state.CFG.cloud["default_data_class"]), key=cloud.class_index)
    if cloud.class_index(requested) > cloud.class_index(session["data_class"]):
        raise ManagedError(403, "input data class exceeds immutable session classification")
    req.data_class, req.execution = session["data_class"], "local"
    cfg = settings()
    # Input identity includes inference-affecting options, excluding transport and
    # model selection: retries/model switches must not duplicate persisted input.
    fingerprint = hashlib.sha256(encoded({k: v for k, v in body.items() if k not in ("model", "stream", "keep_alive")})).hexdigest()
    claim = await store.claim(owner, sid, req.session["event_id"], fingerprint, copy.deepcopy(body["messages"]), cfg)
    turn = Turn(request, req, store, owner, sid, claim["lease"], cfg)
    request[KEY] = turn
    turn.heartbeat = asyncio.create_task(turn.renew())
    turn.context = await store.context(owner, sid, cfg)
    body["messages"] = compose(turn.context)
    req.require |= {"tools"} if any(m.get("tool_calls") or m.get("role") in ("tool", "function") for m in body["messages"]) else set()


class Turn:
    def __init__(self, request, req, store, owner, sid, lease, cfg):
        self.request, self.req, self.store, self.owner, self.sid, self.lease, self.cfg = request, req, store, owner, sid, lease, cfg
        self.heartbeat = None
        self.context = None
        self.finished = False
        self.lost = False

    def check(self):
        if self.lost:
            raise SessionConflict("managed turn lease lost")
        if not settings().enabled or not state.CFG.session_store.enabled:
            raise ManagedError(503, "managed session configuration changed")
        if principal(self.request) != self.owner or not session_api.allowed_project(self.owner, self.context["session"]["project"]):
            raise ManagedError(403, "session policy changed")
        client = auth.client_auth_cfg()["clients"][self.owner]
        if cloud.class_index(self.req.data_class) > cloud.class_index(self.context["session"]["data_class"]):
            raise ManagedError(403, "memory/request data class exceeds session classification")
        if cloud.class_index(client.get("data_class") or state.CFG.cloud["default_data_class"]) > cloud.class_index(self.context["session"]["data_class"]):
            raise ManagedError(403, "session data policy changed")

    async def renew(self):
        try:
            while True:
                await asyncio.sleep(self.cfg.lease_s / 3)
                await self.store.renew(self.owner, self.sid, self.lease, self.cfg.lease_s)
        except ERRORS:
            self.lost = True
            metrics._counter("skirnir_context_events_total", {"operation": "lease", "outcome": "lost"})

    async def target(self, body, tier, ctx):
        self.check()
        if tier.get("cloud"):
            raise ManagedError(403, "managed session inference is local only")
        ctx = min(ctx or tier["num_ctx"], tier["num_ctx"])
        model = state.CFG.catalog_twin(tier["model"]) or {}
        if model.get("context_tokens"):
            ctx = min(ctx, int(model["context_tokens"]))
        opts = dict(body.get("options") or {})
        output = opts.get("num_predict", self.cfg.reserve_tokens)
        if type(output) is not int:
            raise ValueError("managed num_predict must be an integer")
        if output <= 0:
            output = self.cfg.reserve_tokens
        budget = ctx - output - self.cfg.safety_tokens
        if budget <= 0:
            raise SessionCapacity("output and safety reserves exceed target context")
        messages, compacted = project(self.context, body, budget, self.cfg, tier["model"], self.req.memory_context, state.CFG.limits["max_messages"])
        if compacted:
            version = await self.store.compact(self.owner, self.sid, self.lease, compacted["through_seq"], compacted["payload"])
            self.context["compaction"] = version
            self.context["messages"] = [row for row in self.context["messages"] if row["seq"] > version["through_seq"]]
            metrics._counter("skirnir_context_events_total", {"operation": "compaction", "outcome": "ok"})
        opts.update(num_predict=output, num_ctx=ctx)
        body.update(messages=messages, options=opts)

    async def commit(self, capture):
        if self.finished:
            return
        self.check()
        if not capture.done:
            raise ManagedError(502, "incomplete managed response; raw input retained")
        transport = self.request.transport
        if transport is None or transport.is_closing():
            raise ManagedError(502, "managed response interrupted; raw input retained")
        await self.store.finish_turn(self.owner, self.sid, self.lease, capture.message)
        self.finished = True
        metrics._counter("skirnir_context_events_total", {"operation": "turn", "outcome": "completed"})

    async def close(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            await asyncio.gather(self.heartbeat, return_exceptions=True)
        if not self.finished:
            try:
                await self.store.fail_turn(self.owner, self.sid, self.lease)
            except ERRORS:
                pass  # Lease expires independently; raw input remains durable.
            metrics._counter("skirnir_context_events_total", {"operation": "turn", "outcome": "incomplete"})


class AssistantCapture:
    """Capture the canonical native response after tool rescue, before shape conversion."""
    def __init__(self, limit):
        self.limit, self.size, self.done = limit, 0, False
        self.message = {"role": "assistant", "content": ""}

    def observe(self, result):
        if self.done or not isinstance(result, dict) or result.get("error"):
            raise ManagedError(502, "invalid managed response")
        message = result.get("message") or {}
        if not isinstance(message, dict):
            raise ManagedError(502, "invalid managed message")
        try:
            self.size += len(encoded(message))
        except (ValueError, UnicodeError, RecursionError):
            raise ManagedError(502, "invalid managed JSON content") from None
        if self.size > self.limit:
            raise ManagedError(502, "managed response exceeds persistence limit")
        for key in ("content", "thinking"):
            value = message.get(key, "")
            if not isinstance(value, str):
                raise ManagedError(502, "invalid managed text")
            if value:
                self.message[key] = self.message.get(key, "") + value
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            raise ManagedError(502, "invalid managed tool calls")
        for call in calls:
            # Native Ollama sends complete tool calls (arguments are objects).
            # Reject unsupported partial/fragmented calls instead of saving corrupt state.
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ManagedError(502, "invalid managed tool call")
            fn = call["function"]
            if not isinstance(fn.get("name"), str) or not fn["name"].strip() or not isinstance(fn.get("arguments", {}), dict):
                raise ManagedError(502, "managed tool calls require complete native arguments")
            if not call.get("id"):
                call["id"] = "call-" + uuid4().hex
            try:
                session_api.text(call["id"], "tool call id")
            except ValueError:
                raise ManagedError(502, "invalid managed tool identifier") from None
            if any(old["id"] == call["id"] for old in self.message.get("tool_calls", [])):
                raise ManagedError(502, "duplicate or fragmented managed tool call")
            call["index"] = len(self.message.get("tool_calls", []))
            self.message.setdefault("tool_calls", []).append(copy.deepcopy(call))
        self.done = result.get("done") is True


async def lines(content, limit):
    """Bound the complete managed NDJSON reply before buffering arbitrary lines."""
    buffer = bytearray()
    total = 0
    async for chunk in content.iter_chunked(65536):
        total += len(chunk)
        if total > limit:
            raise ManagedError(502, "managed response exceeds persistence limit")
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            yield bytes(line) + b"\n"
    if buffer:
        yield bytes(buffer)
