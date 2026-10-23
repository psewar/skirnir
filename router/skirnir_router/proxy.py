"""Ollama API facing out: accept, assign, and relay requests.

Structure (Refactoring 2026-09-16): `route_request` -> `_route` prepares the request (`_prepare`), selects and
reserves a node (`_Acquire`), and lets `dispatch` execute a tier. `dispatch` is a thin entry point into `_Relay`,
which holds per-request state (times, tokens, result) and maps node-unary, node-stream, and cloud paths into small
methods. Errors before the first byte return None -> `_route` takes the next candidate.
"""

import asyncio
import json
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientTimeout, web

from . import admission, auth, fallback_observation, cloud, decision, context_check, metrics, memory, managed, nodes, openai_node, ops, perf, poll, scheduler, state, toolcall_rescue, wol
from . import request as request_mod
from .common import VERSION, GIB, log, ollama_error, read_json
from .outcome import Problem, bad_gateway, failed

STREAM_PATHS = ("/api/chat", "/api/generate")   # only here does Ollama stream line-by-line NDJSON
CONNECT_TIMEOUT_S = 5                            # Connection setup to the node; the response may take request_timeout_s
NDJSON = "application/x-ndjson"
UPSTREAM_ERRORS = (ClientError, asyncio.TimeoutError, OSError)
CLIENT_FAULT_STATUSES = (400, 404, 413, 422)     # Cloud errors that another node cannot handle better -> to the client


@dataclass(frozen=True, slots=True)
class Refusal(Problem):
    """Request is rejected with HTTP status and plain text (in the client's format, see `err` in _route). A return value
    like any Problem (0.8.9; previously the exception _Reject, which was thrown across the flow)."""
    why: object = None   # scheduler.unavailable(): base code, Retry-After, obstacles per node - for the client


def cloud_client_fault(error):
    """Is a provider error the client's responsibility (return to them) - or can another candidate help (None, continue)?
    An unknown or non-released model is reported by the provider in its structured error code
    (CloudError.model_unavailable) - then continue; until 0.8.8 the router read the message text ("the model ...")."""
    return error.status in CLIENT_FAULT_STATUSES and not error.model_unavailable


def _is_role(role):
    """Role (standard:latest ...) or specific model? Specific models do not get Context/Shadow/Canary."""
    return role["exposed"] in state.CFG.roles


# --- Einstieg -------------------------------------------------------------------------------------------------------

async def handle_infer(request):
    body = await read_json(request)
    if body is None:
        return ollama_error(400, "invalid json")
    return await route_request(request, request.path, body)


async def route_request(request, path, body, shape=None, err=ollama_error):
    """Resolve model (role or specific), select node, relay request. `shape` formats the response
    (None = Ollama format 1:1, otherwise e.g. OpenAI), `err` builds error responses in the client's format.
    Stage 6: with Idempotency-Key (only without Stream) a repeat returns the cached response."""
    # Memory requests must recheck live credentials/project policy on every call.
    # Do not put scoped recall results into the legacy unscoped response cache.
    rb = body.get("routing")
    policy = (auth.client_auth_cfg().get("clients", {}).get(request.get("client")) or {}).get("memory") or {}
    scoped = (isinstance(rb, dict) and (rb.get("memory") or rb.get("session"))) or policy.get("strategy") in ("auto", "hybrid", "manual")
    key = None if scoped else ops.idem_key(request, body, path)
    if not key:
        return await _route(request, path, body, shape, err)
    cached = await ops.idem_wait(key)
    if cached is not None:
        return ops.idem_replay(cached)
    finished = False
    try:
        resp = await _route(request, path, body, shape, err)
        ops.idem_finish(key, resp)
        finished = True
        return resp
    finally:
        if not finished:   # aborted or error: the key may recompute on the next attempt
            ops.idem_abort(key)


async def _route(request, path, body, shape, err):
    try:
        return await _route_inner(request, path, body, shape, err)
    finally:
        turn = request.get(managed.KEY)
        if turn is not None:
            await turn.close()


async def _route_inner(request, path, body, shape, err):
    prepared = await _prepare(request, path, body)
    if failed(prepared):
        return err(prepared.status, prepared.message)
    name, role, tiers, req = prepared
    req.priority = _priority_for(req, role, request)
    acquire = _Acquire(name, role, tiers, req, _client_num_ctx(body), body, path, request.get("client"))
    while True:
        pick = await acquire.next_candidate()
        if failed(pick):
            return err(pick.status, pick.message, getattr(pick, "why", None))
        tier_idx, tier, ctx, node = pick
        result = await dispatch(request, path, body, role, tier, ctx, node, tier_idx, shape, req)
        if isinstance(result, Problem):   # managed turn could not be fitted to this tier
            return err(result.status, result.message)
        if result is not None:
            if _is_role(role) and not req.context_sensitive:
                ops.maybe_shadow(role, body, node, req)   # Stage 6: shadow run after the response, client notices nothing
            return result
        # None = error before the first byte -> next candidate


# --- Preparation: Name, routing-block, Role, Tiers ----------------------------------------------------------------

async def _prepare(request, path, body):
    """Everything decidable without a node. Returns (name, role, tiers, req) or a Refusal."""
    name = _requested_name(body)
    if failed(name):
        return name
    req = request_mod.parse(body, path, request.headers)   # Stage 1: routing-block, derived requirements
    if failed(req):
        return Refusal(400, req.message)
    policy = (auth.client_auth_cfg().get("clients", {}).get(request.get("client")) or {}).get("memory") or {}
    req.context_sensitive = bool(req.memory or req.session or policy.get("strategy") in ("auto", "hybrid", "manual"))
    too_big = request_mod.check_limits(body)
    if too_big:
        return Refusal(413, too_big)
    name = await _decide_role(request, path, body, name, req)
    if failed(name):
        return name
    role, tiers = scheduler.resolve_tiers(name, body)
    if role is None:
        return Refusal(404, f"model '{name}' not found")
    decider = decision.active()
    if decider and req.decision is None:
        decider.record_client_choice(body, path, role["name"], request.get("client"), req.request_id)
    denied = auth.client_allows(request, role, concrete=not _is_role(role))
    if denied:
        return Refusal(403, denied)
    started = await managed.begin(request, path, body, req)
    if failed(started):
        return Refusal(started.status, "invalid managed session request" if started.status == 400 else started.message)
    prepared = await memory.prepare(request, path, body, req)
    if failed(prepared):
        return Refusal(prepared.status, memory.PREPARE_PUBLIC.get(prepared.status, prepared.message))
    # Recall changes the actual prompt, so limits/cloud scans must see the augmented body.
    if req.memory and not req.session:
        too_big = request_mod.check_limits(body)
        if too_big:
            return Refusal(413, too_big)
    tiers = _with_canary(role, tiers, req)
    tiers = _cloud_ordered(request, body, tiers, req)
    unsatisfiable = _unsatisfiable(name, role, tiers, req)
    if unsatisfiable:
        return unsatisfiable
    return name, role, tiers, req


async def _decide_role(request, path, body, name, req):
    """Auto-role: the Decision Engine selects one of the configured roles; all other names pass through unchanged."""
    decider = decision.active()
    if not decider or name != decider.exposed:
        return name
    result = await decider.decide_body(body, path, request.get("client"), req.request_id)
    req.decision = result.as_dict()
    exposed = decision.exposed_for(result.selected)
    if exposed is None:
        return Refusal(503, f"decision engine chose unknown role {result.selected!r}")
    return exposed


def _requested_name(body):
    """Requested model/role name (without tag -> :latest, if the role has it) or a Refusal."""
    name = body.get("model") or body.get("name")
    if not name:
        return Refusal(400, "model is required")
    if ":" not in name and name not in state.CFG.roles and f"{name}:latest" in state.CFG.roles:
        name = f"{name}:latest"
    return name


def _with_canary(role, tiers, req):
    """Stage 6: drawn -> Canary tier to the front, rest remains as fallback."""
    ct = ops.canary_tier(role) if _is_role(role) else None
    if not ct:
        return tiers
    req.canary = ct["model"]
    return [ct] + list(tiers)


def _cloud_ordered(request, body, tiers, req):
    """Stage 5: limits for the cloud tiers of this request; execution=cloud pulls them forward."""
    if not any(t.get("cloud") for t in tiers):
        return tiers
    req.cloud_block = cloud.request_block_reason(req, body, request.get("client"))
    if req.execution == "cloud" and not req.cloud_block:
        return [t for t in tiers if t.get("cloud")] + [t for t in tiers if not t.get("cloud")]
    return tiers


def _unsatisfiable(name, role, tiers, req):
    """No tier can do what the Request demands -> Refusal 400 (Request error, not a capacity issue like 503);
    otherwise None."""
    blocked = [req.tier_blocked(t) for t in tiers]
    if not all(blocked):
        return None
    reasons = "; ".join(sorted({f"{t['model']}: {b}" for t, b in zip(tiers, blocked, strict=True)}))
    state.remember({"event": "unsatisfiable", "role": role["name"], "request_id": req.request_id, "reasons": reasons})
    if _is_role(role):
        return Refusal(400, f"no tier of '{name}' can serve this request ({reasons})")
    return Refusal(400, f"model '{name}' cannot serve this request ({reasons})")


def _client_num_ctx(body):
    ctx = (body.get("options") or {}).get("num_ctx")
    return int(ctx) if ctx else None


def _priority_for(req, role, request):
    """Stage 3: Priority class = routing.priority, otherwise that of the role; the Client identity caps it upwards."""
    prio = req.priority or role.get("priority") or "normal"
    client = (auth.client_auth_cfg().get("clients") or {}).get(request.get("client") or "") or {}
    cap = client.get("max_priority")
    if cap and admission.PRIORITIES[prio] < admission.PRIORITIES[cap]:
        return cap
    return prio


# --- Select node and get space (Scheduler + Admission + WOL) -------------------------------------------------

class _Acquire:
    """Candidate selection per attempt: ask Scheduler, wait in the priority queue if the node is full, wake
    exhausted candidates once. Remembers attempted nodes and wait time for routing.queued_ms."""

    def __init__(self, name, role, tiers, req, client_ctx, body=None, path=None, client=None):
        self.name, self.role, self.tiers, self.req, self.client_ctx = name, role, tiers, req, client_ctx
        self.body, self.path, self.client = body, path, client   # for observation mode (fallback_observation)
        self.tried = set()
        self.woke = False
        self.waited_loads = set()   # (node, model) for which loading has already been waited on
        self.t_enq = None
        # Wait budget: client's deadline_ms, otherwise per priority class (interactive short, batch long)
        self.deadline_at = time.time() + (req.deadline_ms / 1000.0 if req.deadline_ms else admission.max_wait_s(req.priority))

    async def next_candidate(self):
        """(tier_idx, tier, ctx, node) of the next candidate, or a Refusal (503 with reason), if none remain."""
        while True:
            now = time.time()
            pick = scheduler.choose(self.role, self.tiers, self.client_ctx, now, self.tried, self.req)
            if pick is None:
                if await self._wait_for_load(now):
                    continue   # Loading finished (or deadline reached): re-select
                refused = await self._wake_or_refuse()
                if refused:
                    return refused
                continue
            node, model = pick[3], pick[1]["model"]
            if admission.saturated(node, model):
                refused = await self._wait_for_slot(node, now, model)
                if refused:
                    return refused
                continue   # re-select: space free, node gone or another one meanwhile better
            if self.t_enq is not None:
                self.req.queued_ms = (time.time() - self.t_enq) * 1000
            self.tried.add(node.name)
            return pick

    async def _wait_for_load(self, now):
        """No candidate, but a matching node is currently loading a model (VRAM claim from announce_load): wait for the end
        of loading instead of immediate 503. Measured 2026-09-25: when switching qwen3.6 -> gemma4:26b, ~30 s long
        all further requests got 'no node available' (778 of 1142), because the old model counted as loaded until the
        next /api/ps poll and the new one's claim broke the budget. Waits per (node, model) once, until /api/ps shows
        the model or the claim is gone (then two more poll intervals), at most until the Admission deadline."""
        found = scheduler.loading_for(self.tiers, now, self.tried)
        if found is None or now >= self.deadline_at:
            return False
        node, model = found
        if (node.name, model) in self.waited_loads:
            return False   # already waited, still no space: continue normally (wake / 503)
        self.waited_loads.add((node.name, model))
        if self.t_enq is None:
            self.t_enq = now
        state.remember({"event": "wait_load", "role": self.role["name"], "node": node.name, "model": model,
                        "request_id": self.req.request_id})
        settle = 2 * state.CFG.poll_s + 1
        t_gone = None
        while time.time() < self.deadline_at:
            if node.is_loaded(model):
                break
            claim = node.loading.get(model)
            if claim is None or time.time() >= claim[1]:
                t_gone = t_gone or time.time()
                if time.time() - t_gone >= settle:
                    break
            await asyncio.sleep(0.5)
        return True

    async def _wake_or_refuse(self):
        """Candidates exhausted -> one wake attempt (None: woken, re-select), then a Refusal 503."""
        node = None if self.woke else scheduler.wakeable_for(self.tiers)
        if node is None:
            why = scheduler.unavailable(self.tiers, self.client_ctx, time.time(), self.tried)
            state.remember({"event": "no_node", "role": self.role["name"], "tried": sorted(self.tried),
                            "code": why["code"], "detail": why["detail"], "blockers": why["blockers"]})
            return Refusal(503, f"no node available for model '{self.name}': {why['detail']}", why=why)
        self.woke = True
        if not await wol.wake(node):
            return Refusal(503, f"no node available for model '{self.name}' (wake of {node.name} failed)")
        return None

    async def _wait_for_slot(self, node, now, model=None):
        """Stage 3: Node (or model, max_parallel) full -> wait here (with priority and aging), not in Ollama's
        queue without priority - that reloads the Runner for models without parallel operation. None: continue selecting,
        otherwise a Refusal 503 (deadline reached, queue full)."""
        limit = state.CFG.max_parallel(model) if model else None
        model_full = bool(limit) and node.inflight < node.effective_max_inflight()
        if self.t_enq is None:
            self.t_enq = now
            state.remember({"event": "queued", "role": self.role["name"], "node": node.name, "priority": self.req.priority,
                            "request_id": self.req.request_id, "inflight": node.inflight, "model": model,
                            "reason": "max_parallel" if model_full else "max_inflight"})
            if _is_role(self.role) and self.body is not None and not self.req.context_sensitive:   # 0.6.7: would a deeper free tier have sufficed? (protocol only)
                fallback_observation.start(self.body, self.path, self.role, self.tiers, self.req, node, model,
                                           "max_parallel" if model_full else "max_inflight", self.client)
        if now >= self.deadline_at:
            waited = round((now - self.t_enq) * 1000)
            state.remember({"event": "admission_timeout", "role": self.role["name"], "node": node.name, "priority": self.req.priority,
                            "request_id": self.req.request_id, "waited_ms": waited, "model": model})
            what = (f"model {model} on {node.name} runs {node.inflight_models[model]}/{limit} requests (no parallel requests)"
                    if model_full else f"node {node.name} has {node.inflight}/{node.effective_max_inflight()} requests running")
            return Refusal(503, f"queue deadline exceeded after {waited} ms (priority {self.req.priority}, {what})")
        waited = await admission.wait_for_slot(node, self.req.priority, self.t_enq, self.deadline_at, self.req.request_id,
                                               self.role["name"], model)
        if failed(waited):   # Warteschlange voll
            return Refusal(waited.status, waited.message)
        return None


# --- Execute a tier ---------------------------------------------------------------------------------------------

async def dispatch(request, path, body, role, tier, ctx, node, tier_idx, shape=None, req=None):
    """Execute a tier on a node or cloud provider. None = error before the first byte -> next candidate."""
    turn = request.get(managed.KEY)
    if turn is not None:
        targeted = await turn.target(body, tier, ctx)
        if failed(targeted):
            return targeted
        ctx = body["options"]["num_ctx"]
    relay = _Relay(request, path, body, role, tier, ctx, node, tier_idx, shape, req or request_mod.Routing.fresh())
    return await relay.run()


def _backend_body(body, role, tier, ctx, node):
    """Body for the node: specific model, context, keep_alive based on node state. Returns (out, ctx)."""
    out = dict(body)
    out["model"] = tier["model"]
    opts = dict(out.get("options") or {})
    if _is_role(role) or "num_ctx" in opts:
        # Already loaded with a larger context (up to what the tier allows): use that. A smaller
        # num_ctx forces models without runner sharing to reload (qwen3.8:27b, 2026-09-30: 10 s per switch between a
        # client with 65k and one with 131k), and the VRAM for the larger context is already occupied anyway.
        lc = node.loaded_context(tier["model"]) if hasattr(node, "loaded_context") else None
        if _is_role(role) and lc and ctx < lc <= tier["num_ctx"]:
            ctx = lc
        opts["num_ctx"] = ctx
    else:
        # specific model without context specification: use the context it is already loaded with,
        # otherwise Ollama reloads with its default (OLLAMA_CONTEXT_LENGTH) -> 15-20 s and the HA model is gone
        ctx = node.loaded_context(tier["model"]) or None
        if ctx:
            opts["num_ctx"] = ctx
    out["options"] = opts
    out["keep_alive"] = state.CFG.keep_alive.get(node.state, "5m")   # Number (seconds, -1) or duration like 5m - Ollama accepts both
    return out, ctx


async def _awaited(awaitable):
    """nreq returns an awaitable object (aiohttp or TunnelRequest), not a coroutine; it needs one as a Task."""
    return await awaitable


def _failed_before_first_byte(status):
    """5xx (except 501) and 404 are node problems: next candidate. 4xx and 501 go to the client."""
    return (status >= 500 and status != 501) or status == 404


class _Relay:
    """State and flow of an assignment: bookkeeping at the node, forwarding, tool call rescue, metrics."""

    def __init__(self, request, path, body, role, tier, ctx, node, tier_idx, shape, req):
        self.request, self.path, self.role, self.tier, self.node = request, path, role, tier, node
        self.tier_idx, self.shape, self.req = tier_idx, shape, req
        self.model, self.exposed = tier["model"], role["exposed"]
        self.out, self.ctx = _backend_body(body, role, tier, ctx, node)
        if req.session:
            self.ctx = ctx
            self.out["options"]["num_ctx"] = ctx
        self.stream = bool(self.out.get("stream", True)) and path in STREAM_PATHS
        # Ollama does not restrict the tools path and silently discards a failed call;
        # measured, qwen3-coder:30b loses 80% of its calls above ~12k tokens. See toolcall_rescue.
        self.rescue = state.CFG.toolcall_rescue and toolcall_rescue.active_for(self.out)
        self.is_cloud = node.is_cloud
        # Does the request fit in num_ctx? Ollama truncates silently otherwise; confirmed after the response (context_check)
        self.estimated_tokens = context_check.before_request(self.out, self.ctx, self.is_cloud)
        self.warm = node.is_loaded(self.model)
        self.t0 = time.time()
        self.outcome = None          # Stage 3: ok | error | timeout | structured_error (perf_outcome + Breaker)
        self.ptoks = self.ctoks = 0  # Stage 4: Tokens for metrics/usage
        self.ttft_s = None
        self.info = self.headers = None
        self.resp = None             # Response to the client as soon as the stream is open (_open_stream)
        self.memory_capture = memory.Capture(request, req, body) if req.memory and req.memory["retain"] else None
        self.context_sensitive = req.context_sensitive
        self.managed_turn = request.get(managed.KEY)
        self.managed_capture = managed.AssistantCapture(self.managed_turn.cfg.max_response_bytes) if self.managed_turn else None
        self.managed_problem = None  # set by _render_chunks when a managed chunk cannot be persisted
        self.obs_task = None         # 0.6.7: Context for anfragen.jsonl in the background (fallback_observation, observe_all)

    async def run(self):
        self._begin()
        try:
            if self.req.memory:
                service = self.request.app.get(memory.SERVICE_KEY)
                allowed = service is not None and service.current(self.req.memory_scope, read=self.req.memory["recall"], write=self.req.memory["retain"])
                if not allowed:
                    self.outcome = "error"
                    return self.shape.error(403, "memory policy changed") if self.shape else ollama_error(403, "memory policy changed")
            if self.is_cloud:   # Stage 5: Provider instead of node
                return await self._via_cloud()
            if self.node.serves_openai(self.model):   # model of an OpenAI-compatible server (openai_node.py)
                return await self._via_openai_node()
            return await self._via_node()
        except (*UPSTREAM_ERRORS, ConnectionResetError) as e:  # exception-ok: network to the node/client mid-answer
            # Error after the first byte: nothing left to save, connection ends
            log.warning("stream from %s aborted: %s", self.node.name, e)
            if self.resp is not None and hasattr(self.shape, "stream_error"):
                return await self._end_with_error(502, f"upstream {self.node.name} aborted")
            # exception-ok: aiohttp's way to end a handler with 502 when no error event fits the client's format
            raise web.HTTPBadGateway(text=json.dumps({"error": f"upstream {self.node.name} aborted"})) from e
        finally:
            try:
                if self.memory_capture is not None:
                    await self.memory_capture.flush(self.outcome)
            finally:
                self._finish()

    # -- Buchfuehrung --

    def _begin(self):
        node, model = self.node, self.model
        node.inflight += 1
        node.inflight_models[model] += 1
        node.last_used[model] = self.t0   # for the residency rule: is the evictee still needed?
        if not self.warm:
            node.announce_load(model, self.ctx or self.tier["num_ctx"])
        via = self.shape.name if self.shape else "ollama"
        self.info = self.req.info(self.role, self.tier_idx, self.tier, self.ctx, node, self.warm, via, self.request.get("client"))
        self.headers = request_mod.headers_for(self.info)
        if hasattr(self.shape, "bind"):   # Anthropic: Message-ID = Request-ID, model = Name known to the client
            self.shape.bind(self.req.request_id, self.exposed)
        self.obs_task = None if self.context_sensitive else fallback_observation.begin(self.path, self.out)
        request_mod.remember_session(self.req.session_id, node.name, model)
        reason = "loaded" if self.warm else "cold"
        log.info("route %s %s -> tier%d %s ctx=%s node=%s(%s,%s) inflight=%d req=%s%s", self.path, self.role["name"], self.tier_idx,
                 model, self.ctx, node.name, node.state, reason, node.inflight, self.req.request_id,
                 f" skipped={len(self.req.skipped)}" if self.req.skipped else "")
        state.remember({"event": "route", "path": self.path, "role": self.role["name"], "tier": self.tier_idx, "model": model,
                        "ctx": self.ctx, "node": node.name, "node_state": node.state, "warm": self.warm, "via": via,
                        "request_id": self.req.request_id, "session_id": self.req.session_id, "client": self.request.get("client"),
                        "reason": self.req.reason, "skipped": len(self.req.skipped), "think": self.out.get("think"),
                        "canary": bool(self.req.canary) and model == self.req.canary})

    def _finish(self):
        if self.memory_capture is not None:
            self.memory_capture.finish(self.outcome)
        node, model = self.node, self.model
        node.inflight -= 1
        node.inflight_models[model] -= 1
        node.finish_load(model)
        if self.outcome:
            perf.perf_outcome(model, node.name, self.outcome)
            if self.outcome == "ok":
                node.breaker_ok()
        cost = cloud.cost_chf(model, self.ptoks, self.ctoks) if self.is_cloud and self.outcome == "ok" else 0.0
        if cost:
            cloud.add_spend(node.provider, cost)
        elapsed = time.time() - self.t0
        metrics.observe_request(self.info, self.outcome or "aborted", elapsed, self.ttft_s, self.ptoks, self.ctoks,
                                self.req.queued_ms / 1000.0, cost)
        if self.estimated_tokens and self.outcome == "ok":
            context_check.after_request(self.estimated_tokens, self.ptoks, self.ctx,
                                    {**(self.info or {}), "path": self.path, "request_id": self.req.request_id})
        admission.release(node)   # Stage 3: Space free -> wake up the highest-ranked waiter
        log.info("done %s node=%s %.1fs%s", self.role["name"], node.name, elapsed, f" {cost:.4f} CHF" if cost else "")
        fallback_observation.finish(self.obs_task, {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "request_id": self.req.request_id, "client": self.request.get("client"),
            "role": self.role["name"], "is_role": _is_role(self.role), "model": model, "node": node.name, "path": self.path,
            "via": self.shape.name if self.shape else "ollama", "stream": self.stream, "priority": self.req.priority,
            "think": self.out.get("think"), "tools": len(self.out.get("tools") or []), "messages": len(self.out.get("messages") or []),
            "num_ctx": self.ctx, "warm": self.warm, "queued_s": round(self.req.queued_ms / 1000.0, 2), "duration_s": round(elapsed, 2),
            "ttft_s": round(self.ttft_s, 2) if self.ttft_s else None, "prompt_tokens": self.ptoks, "output_tokens": self.ctoks,
            "outcome": self.outcome or "aborted"})
        if not self.warm and not self.is_cloud:
            # register a freshly loaded model immediately, not at the next 5-s poll:
            # otherwise the next request is incorrectly treated as a cold start for up to 5 s
            state.spawn(poll.poll_node(node))

    def _fail(self, outcome, breaker_reason):
        """Error before the first byte: Breaker counts, caller takes the next candidate."""
        self.outcome = outcome
        self.node.breaker_fail(time.time(), breaker_reason)
        return None

    # -- Node --

    async def _via_node(self):
        timeout = ClientTimeout(total=state.CFG.request_timeout_s, sock_connect=CONNECT_TIMEOUT_S, sock_read=state.CFG.request_timeout_s)
        try:
            upstream = await self._connect(timeout)
        except UPSTREAM_ERRORS as e:  # exception-ok: network; a node can fail between choosing and connecting
            log.warning("node %s connect failed: %s", self.node.name, e)
            self._fail("timeout" if isinstance(e, asyncio.TimeoutError) else "error", f"connect: {type(e).__name__}")
            if self.resp is not None:   # Stream already open (Keep-alive): no fallback candidate anymore
                return await self._end_with_error(502, f"upstream {self.node.name} failed: {type(e).__name__}")
            return None
        async with upstream:
            if upstream.status >= 400 and self.resp is not None:
                return await self._late_upstream_error(upstream)
            if _failed_before_first_byte(upstream.status):
                return await self._node_failed(upstream)
            if upstream.status >= 400:
                return await self._pass_through_error(upstream)
            if not self.stream:
                return await self._complete_unary(upstream)
            return await self._complete_stream(upstream)

    async def _connect(self, timeout):
        """Submit request and wait for the node's headers. Ollama sends them only with the first token, after
        Prefill (196k context: up to ~2 min). Clients with Keep-alive (Anthropic-SSE: Claude Code, Proxys) therefore get the
        stream start after `keepalive_s` without a response and then pings; from then on there is no fallback candidate.
        Fast errors (connection, 5xx) almost always come before and retain the fallback."""
        call = nodes.nreq(self.node, "post", self.path, json=self.out, timeout=timeout)
        every = getattr(self.shape, "keepalive_s", 0) if self.stream else 0
        if not every:
            return await call
        task = asyncio.ensure_future(_awaited(call))
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=every)
                if done:
                    return task.result()   # Upstream errors go to _via_node as without Keep-alive
                await self._open_stream()
                await self.resp.write(self.shape.ping())
        finally:
            if not task.done():   # Client gone (abort of this handler): do not let the request to the node continue
                task.cancel()

    async def _late_upstream_error(self, upstream):
        """Node rejects after the stream is already open: error as event in the stream instead of HTTP status."""
        txt = (await upstream.text())[:500]
        log.warning("node %s returned %s after keep-alive: %s", self.node.name, upstream.status, "context response redacted" if self.context_sensitive else txt[:200])
        if upstream.status == 404:
            self.node.models.discard(self.model)   # like _node_failed: node does not have the model (anymore)
        elif _failed_before_first_byte(upstream.status):
            self._fail("error", f"HTTP {upstream.status}")
        try:
            j = json.loads(txt)
        except ValueError:  # exception-ok: upstream data; Ollama errors are JSON, anything else (proxy, cut off) stays text
            j = None
        msg = (j.get("error") if isinstance(j, dict) else None) or txt
        return await self._end_with_error(upstream.status, f"upstream {self.node.name}: {msg}")

    async def _end_with_error(self, status, msg):
        """Close open stream with `event: error`. If the client is already gone, there is no one left to notify."""
        self.outcome = self.outcome or "error"
        transport = self.request.transport
        if transport is not None and not transport.is_closing():
            data = self.shape.stream_error(status, msg) if self.shape else (json.dumps({"error": msg}) + "\n").encode()
            await self.resp.write(data)
            await self.resp.write_eof()
        return self.resp

    async def _node_failed(self, upstream):
        txt = await upstream.text()
        log.warning("node %s returned %s before first byte: %s", self.node.name, upstream.status, "context response redacted" if self.context_sensitive else txt[:200])
        if upstream.status == 404:
            self.node.models.discard(self.model)   # Node does not have the model (anymore); not a Breaker case
            return None
        return self._fail("structured_error" if self.out.get("format") else "error", f"HTTP {upstream.status}")

    async def _pass_through_error(self, upstream):
        """4xx and 501 ("does not support embeddings": model cannot do that, another node cannot either) -> to the client."""
        txt = await upstream.read()
        if not self.shape:
            return web.Response(status=upstream.status, body=txt, content_type="application/json", headers=self.headers)
        try:
            parsed = json.loads(txt)
        except ValueError:  # exception-ok: upstream data; json.loads is the only full validity check
            parsed = None
        msg = (parsed.get("error") if isinstance(parsed, dict) else None) or txt.decode(errors="replace")
        return self.shape.error(upstream.status, str(msg))

    async def _answer_problem(self, problem):
        """Managed-turn problem: error event inside an already open stream, otherwise an error response."""
        self.outcome = "error"
        if self.resp is not None:
            return await self._end_with_error(problem.status, problem.message)
        return self._client_error(problem.status, problem.message)

    async def _complete_unary(self, upstream):
        if self.managed_capture is not None:
            data = bytearray()
            async for chunk in upstream.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > self.managed_turn.cfg.max_response_bytes:
                    return await self._answer_problem(bad_gateway("managed response exceeds persistence limit"))
        else:
            data = await upstream.read()
        try:
            j = json.loads(data)
        except ValueError:  # exception-ok: upstream data; json.loads is the only full validity check
            if self.managed_capture is not None:
                return await self._answer_problem(bad_gateway("invalid managed upstream JSON"))
            if self.shape:
                return self.shape.error(502, f"upstream {self.node.name}: invalid json")
        else:
            if isinstance(j, dict):
                if self.rescue:
                    self._rescue_unary(j)
                if self.memory_capture is not None:
                    self.memory_capture.observe(j)
                if self.managed_capture is not None:
                    problem = self.managed_capture.observe(j)
                    if not failed(problem):
                        problem = await self.managed_turn.commit(self.managed_capture)
                    if failed(problem):
                        return await self._answer_problem(problem)
                self._record_stats(j, ttft=None)
                data = self._finalize_json(j)
            else:
                if self.managed_capture is not None:
                    return await self._answer_problem(bad_gateway("invalid managed upstream response"))
                data = self.shape.complete(j) if self.shape else json.dumps(j).encode()
        self.outcome = "ok"
        return web.Response(status=200, body=data, content_type="application/json", headers=self.headers)

    async def _complete_stream(self, upstream):
        resp = await self._open_stream()
        rescuer = toolcall_rescue.StreamRescuer(self.out.get("tools")) if self.rescue else None
        first_at = None
        terminal = b""
        lines = managed.lines(upstream.content, self.managed_turn.cfg.max_response_bytes) if self.managed_turn else upstream.content
        async for line in lines:
            if isinstance(line, Problem):   # managed.lines: reply exceeded the persistence limit
                return await self._answer_problem(line)
            if not line.strip():
                continue
            first_at = first_at or time.time()
            chunk = self._render_chunks(line, rescuer, first_at)
            if self.managed_problem is not None:
                return await self._answer_problem(self.managed_problem)
            if self.managed_capture is not None:
                if chunk is None:
                    return await self._answer_problem(bad_gateway("invalid managed stream JSON"))
                if self.managed_capture.done:
                    terminal = chunk
                    continue  # Hold success until EOF and the database commit.
            if chunk is None:            # no JSON: forward raw in Ollama format, omit in foreign format
                chunk = b"" if self.shape else line
            if chunk:
                await resp.write(chunk)
        self._note_stream_rescue(rescuer)
        if self.managed_capture is not None:
            problem = await self.managed_turn.commit(self.managed_capture)
            if failed(problem):
                return await self._answer_problem(problem)
            if terminal:
                await resp.write(terminal)
        if self.shape:
            await resp.write(self.shape.tail())
        await resp.write_eof()
        self.outcome = self.outcome or "ok"   # an error in the middle of the stream (_stream_error) remains an error
        return resp

    def _render_chunks(self, line, rescuer, first_at):
        """Bring an NDJSON chunk into the client format. None = no JSON. The rescuer can hold back a chunk
        (empty list) or split it into two (held text + rest) - hence the loop; without rescue it is a single pass."""
        try:
            j = json.loads(line)
        except ValueError:  # exception-ok: upstream data; json.loads is the only full validity check
            return None
        if self.managed_capture is not None and (not isinstance(j, dict) or j.get("error")):
            self.managed_problem = bad_gateway("managed upstream stream failed")
            return None
        if isinstance(j, dict) and j.get("error") and "message" not in j and not j.get("done"):
            return self._stream_error(line, str(j["error"]))
        if self.memory_capture is not None:
            self.memory_capture.observe(j)
        out = b""
        for jj in (rescuer.chunk(j) if rescuer else [j]):
            if self.managed_capture is not None:
                problem = self.managed_capture.observe(jj)
                if failed(problem):
                    self.managed_problem = problem
                    return None
            if "model" in jj:
                jj["model"] = self.exposed
            if jj.get("done"):
                self._record_stats(jj, ttft=first_at - self.t0 - (jj.get("load_duration") or 0) / 1e9)
                if self.req.explicit:
                    jj["routing"] = self.info
            out += (self.shape.chunk(jj) if self.shape else (json.dumps(jj) + "\n").encode()) or b""
        return out

    def _stream_error(self, line, msg):
        """Ollama aborts a response mid-stream with {"error": ...} - measured 2026-10-02: "XML syntax error on line 3:
        unexpected EOF", the model had written a tool call with broken XML, Ollama discarded the whole response.
        Until 0.6.3 the router passed the line through and counted the request as ok; now it counts as an error, appears in the log
        and the decision protocol, and foreign formats get an error event in their form. Not a Breaker case: the node
        is working, the model's output was broken."""
        self.outcome = "error"
        safe_message = "context response redacted" if self.context_sensitive else msg[:200]
        log.warning("stream from %s: Ollama error in the middle of the answer (%s, req=%s): %s", self.node.name, self.model,
                    self.req.request_id, safe_message)
        state.remember({"event": "stream_error", "node": self.node.name, "model": self.model, "role": self.role["name"],
                        "client": self.request.get("client"), "request_id": self.req.request_id, "error": safe_message})
        if not self.shape:   # Ollama clients get the line verbatim, as from Ollama itself
            return line if line.endswith(b"\n") else line + b"\n"
        if hasattr(self.shape, "stream_error"):
            return self.shape.stream_error(502, f"upstream {self.node.name}: {msg}")
        return b""

    # -- Cloud (stage 5) --

    async def _via_cloud(self):
        """Request to a cloud provider (cloud.run), response in Ollama format to the client. None = next candidate
        (provider error before first byte, Breaker counts it)."""
        try:
            cinfo, result = await cloud.run(self.node, self.path, self.out, self.model, self.stream)
        except cloud.CloudError as e:  # exception-ok: provider HTTP error (raised in the stream generator too, see CloudError)
            log.warning("%s: %s -> HTTP %s: %s", self.node.name, self.model, e.status, e)
            self._fail("timeout" if e.status == 408 else "error", f"HTTP {e.status}")
            if cloud_client_fault(e):
                return self._client_error(e.status, str(e))
            return None
        except UPSTREAM_ERRORS as e:  # exception-ok: network to the provider
            log.warning("%s connect failed: %s", self.node.name, e)
            return self._fail("timeout" if isinstance(e, asyncio.TimeoutError) else "error", f"connect: {type(e).__name__}")
        if not self.stream:
            if self.memory_capture is not None:
                self.memory_capture.observe(result)
            self.ptoks, self.ctoks = cinfo["p_tok"], cinfo["c_tok"]
            self.outcome = "ok"
            return web.Response(status=200, body=self._finalize_json(result, force_model=True), content_type="application/json", headers=self.headers)
        return await self._cloud_stream(result, cinfo)

    # -- OpenAI-compatible node (openai_node.py) --

    async def _via_openai_node(self):
        """Request to a node whose agent fronts an OpenAI-compatible server: translated like a cloud request, sent
        through the tunnel. None = next candidate (node problem before the first byte, a request this node does not
        serve, or a server that lacks the embeddings endpoint)."""
        if self.path not in openai_node.PATHS:
            log.info("%s: %s not served by an OpenAI-compatible node -> next candidate", self.node.name, self.path)
            return None
        if not self.warm and not await openai_node.ensure_loaded(self.node, self.model):
            # server stopped for the GPU and could not be started (or not in time): not a fault of the node's server
            log.info("%s: %s not loaded -> next candidate", self.node.name, self.model)
            return None
        try:
            cinfo, result = await openai_node.run(self.node, self.path, self.out, self.model, self.stream)
        except cloud.CloudError as e:  # exception-ok: the server's HTTP error, same channel as the cloud adapter (CloudError)
            log.warning("%s: %s -> HTTP %s: %s", self.node.name, self.model, e.status, e)
            if e.status == 404 and self.path in cloud.EMBED_PATHS and not e.model_unavailable:
                return None   # the server has no /v1/embeddings: next candidate, the node keeps its chat model
            if e.status == 404 or e.model_unavailable:
                self.node.models.discard(self.model)   # server does not have the model (anymore); not a Breaker case
                return None
            if _failed_before_first_byte(e.status):
                return self._fail("structured_error" if self.out.get("format") else "error", f"HTTP {e.status}")
            self.outcome = "error"
            return self._client_error(e.status, str(e))
        except UPSTREAM_ERRORS as e:  # exception-ok: network/tunnel to the node
            log.warning("node %s connect failed: %s", self.node.name, e)
            return self._fail("timeout" if isinstance(e, asyncio.TimeoutError) else "error", f"connect: {type(e).__name__}")
        if not self.stream:
            if self.memory_capture is not None:
                self.memory_capture.observe(result)
            if self.managed_capture is not None:   # managed turn: persist before answering, as on an Ollama node
                problem = self.managed_capture.observe(result)
                if not failed(problem):
                    problem = await self.managed_turn.commit(self.managed_capture)
                if failed(problem):
                    return await self._answer_problem(problem)
            self.ptoks, self.ctoks = cinfo["p_tok"], cinfo["c_tok"]
            self.outcome = "ok"
            return web.Response(status=200, body=self._finalize_json(result, force_model=True), content_type="application/json", headers=self.headers)
        return await self._cloud_stream(result, cinfo)

    async def _cloud_stream(self, chunks, cinfo):
        """cinfo fills cloud.run only during the stream (usage comes at the end) - read tokens only after write_eof.
        A managed turn (OpenAI-compatible nodes; cloud tiers never get one) captures every chunk and holds the final one
        until the database commit, as _complete_stream does for Ollama."""
        resp = None
        terminal = None
        try:
            async for j in chunks:
                if self.memory_capture is not None:
                    self.memory_capture.observe(j)
                if self.managed_capture is not None and failed(problem := self.managed_capture.observe(j)):
                    return await self._answer_problem(problem)
                if resp is None:
                    resp = await self._open_stream()
                j["model"] = self.exposed
                if j.get("done") and self.req.explicit:
                    j["routing"] = self.info
                line = self.shape.chunk(j) if self.shape else (json.dumps(j) + "\n").encode()
                if self.managed_capture is not None and self.managed_capture.done:
                    terminal = line   # hold success until the commit
                    continue
                if line:
                    await resp.write(line)
        except cloud.CloudError as e:  # exception-ok: provider HTTP error raised by the stream generator (its only channel)
            if resp is None:   # nothing sent yet -> next candidate
                log.warning("%s: %s -> HTTP %s: %s", self.node.name, self.model, e.status, e)
                return self._fail("error", f"HTTP {e.status}")
            if hasattr(self.shape, "stream_error"):
                return await self._end_with_error(502, f"{self.node.name} aborted: {e}")
            # exception-ok: aiohttp's way to end a handler with 502 when no error event fits the client's format
            raise web.HTTPBadGateway(text=json.dumps({"error": f"{self.node.name} aborted: {e}"})) from e
        if resp is None:   # leerer Stream: trotzdem sauber abschliessen
            resp = await self._open_stream()
        if self.managed_capture is not None:
            problem = await self.managed_turn.commit(self.managed_capture)
            if failed(problem):
                return await self._answer_problem(problem)
            if terminal:
                await resp.write(terminal)
        if self.shape:
            await resp.write(self.shape.tail())
        await resp.write_eof()
        self.ptoks, self.ctoks = cinfo["p_tok"], cinfo["c_tok"]
        self.outcome = "ok"
        return resp

    def _client_error(self, status, msg):
        if self.shape:
            return self.shape.error(status, msg)
        return web.Response(status=status, body=json.dumps({"error": msg}).encode(), content_type="application/json", headers=self.headers)

    # -- Gemeinsames --

    async def _open_stream(self):
        """Open stream to client (once; Keep-alive in _connect may do it earlier). Formats with header events
        (Anthropic: message_start) send it immediately."""
        if self.resp is not None:
            return self.resp
        resp = web.StreamResponse(status=200, headers={"Content-Type": self.shape.stream_content_type if self.shape else NDJSON, **self.headers})
        await resp.prepare(self.request)
        self.resp = resp
        if hasattr(self.shape, "head"):
            await resp.write(self.shape.head())
        return resp

    def _finalize_json(self, j, force_model=False):
        """model field set to the name the client knows; routing block only if explicit routing block in request."""
        if force_model or "model" in j:
            j["model"] = self.exposed
        if self.req.explicit:
            j["routing"] = self.info
        return self.shape.complete(j) if self.shape else json.dumps(j).encode()

    def _record_stats(self, j, ttft):
        """Adopt Ollama counters (eval_count ...) for speed stats and token metrics."""
        if not j.get("eval_count"):
            return
        perf.perf_record(self.model, self.node.name, j, ttft)
        self.ttft_s = ttft
        self.ptoks, self.ctoks = int(j.get("prompt_eval_count") or 0), int(j.get("eval_count") or 0)

    # -- Tool-Call-Rettung (Protokoll) --

    def _rescue_unary(self, j):
        jm = j.get("message") or {}
        tool = toolcall_rescue.rescue_message(jm, self.out.get("tools"))
        if tool:
            self._note_rescue(tool, stream=False)
        elif not jm.get("tool_calls") and toolcall_rescue.suspicious(jm.get("content")):
            self._note_lost(stream=False, content=jm.get("content"))

    def _note_stream_rescue(self, rescuer):
        if rescuer is None:
            return
        if rescuer.rescued:
            self._note_rescue(rescuer.rescued, stream=True)
        elif rescuer.lost:
            self._note_lost(stream=True)

    def _note_rescue(self, tool, stream):
        log.warning("tool call rescued%s: %s had written %s as text (req=%s)", " (stream)" if stream else "",
                    self.model, tool, self.req.request_id)
        state.remember({"event": "toolcall_rescue", "node": self.node.name, "model": self.model, "tool": tool,
                        "stream": stream, "request_id": self.req.request_id})

    def _note_lost(self, stream, content=None):
        """Unknown dialect: lost for the client, but no longer silent."""
        sample = f": {(content or '').replace(chr(10), ' ')[:120]}" if content and not self.context_sensitive else ""
        log.warning("tool call LOST (unknown dialect%s): %s req=%s%s", ", stream" if stream else "", self.model,
                    self.req.request_id, sample)
        state.remember({"event": "toolcall_lost", "node": self.node.name, "model": self.model, "stream": stream,
                        "request_id": self.req.request_id})


# --- Auskunft: tags, show, ps, version -------------------------------------------------------------------------------

async def handle_tags(request):
    return web.json_response({"models": tags_list()})


def tags_list():
    """Model list like /api/tags: roles (with details of their tier-1 model) + optional specific models of all nodes."""
    out = []
    seen = set()
    for exposed, role in state.CFG.roles.items():
        first = role["tiers"][0]["model"]
        n = scheduler.any_online_node_with(first)
        d = dict(n.model_details[first]) if n else {}
        out.append({"name": exposed, "model": exposed, "modified_at": d.get("modified_at", "2026-01-01T00:00:00Z"),
                    "size": d.get("size", 0), "digest": d.get("digest", "router-" + role["name"]),
                    "details": d.get("details", {})})
        seen.add(exposed)
    if state.CFG.expose_concrete:
        for n in state.NODES.values():
            for m, d in n.model_details.items():
                if m not in seen:
                    seen.add(m)
                    out.append(d)
    decider = decision.active()
    if decider:   # the auto role is a model to clients like any role
        out.append({"name": decider.exposed, "model": decider.exposed, "modified_at": "2026-01-01T00:00:00Z", "size": 0,
                    "digest": "router-decision", "details": {"family": "router-decision", "options": decider.options}})
    return out


async def handle_show(request):
    body = await read_json(request)
    if body is None:
        return ollama_error(400, "invalid json")
    name = body.get("model") or body.get("name") or ""
    if ":" not in name and f"{name}:latest" in state.CFG.roles:
        name = f"{name}:latest"
    decider = decision.active()
    if decider and name in (decider.exposed, decider.role):   # /show of the auto role: info about the default role
        name = decision.exposed_for(decider.default) or name
    role, tiers = scheduler.resolve_tiers(name, body)
    if role is None:
        return ollama_error(404, f"model '{name}' not found")
    model = tiers[0]["model"]
    if tiers[0].get("cloud"):   # Stage 5: Cloud model has no Ollama - synthetic info
        return web.json_response({"capabilities": request_mod.effective_caps(model) or ["completion"],
                                  "details": {"family": "cloud", "provider": tiers[0]["cloud"]},
                                  "model_info": {}, "modelfile": "", "parameters": ""})
    n = scheduler.any_online_node_with(model)
    if n is None:
        # last resort: node that last had the model, waking it is not worth it for /show
        return ollama_error(503, f"no online node holds '{model}'")
    if n.serves_openai(model):   # OpenAI-compatible server: no /api/show either - synthetic info from the catalog, as for cloud
        return web.json_response({"capabilities": request_mod.effective_caps(model) or ["completion"],
                                  "details": {"family": "openai-compatible", "node": n.name},
                                  "model_info": {}, "modelfile": "", "parameters": ""})
    try:
        async with nodes.nreq(n, "post", "/api/show", json={"model": model, "verbose": bool(body.get("verbose", False))},
                              timeout=ClientTimeout(total=15)) as r:
            data = await r.read()
            return web.Response(status=r.status, body=data, content_type="application/json")
    except UPSTREAM_ERRORS as e:  # exception-ok: network to the node
        return ollama_error(502, f"show via {n.name} failed: {e}")


async def handle_ps(request):
    concrete_to_role = {}
    for exposed, role in state.CFG.roles.items():
        for t in role["tiers"]:
            concrete_to_role.setdefault(t["model"], exposed)
    out = []
    for n in state.NODES.values():
        for m, gib in n.loaded.items():
            role = concrete_to_role.get(m) or next((r for c, r in concrete_to_role.items() if n.same_blob(m, c)), m)
            out.append({"name": role, "model": m, "node": n.name, "size_vram": int(gib * GIB), "size": int(gib * GIB)})
    return web.json_response({"models": out})


async def handle_version(request):
    return web.json_response({"version": f"0.11.0-router{VERSION}"})


async def handle_availability(request):
    """GET /v1/skirnir/availability/{model}: would a request now get a node? Pure view, loads nothing, wakes
    nothing. For clients waiting for gpu_busy who want to know when it continues (e.g. an agent plugin,
    gpu-wait), instead of sending probe requests. Context like without num_ctx: the full tier."""
    name = request.match_info["model"]
    if ":" not in name and name not in state.CFG.roles and f"{name}:latest" in state.CFG.roles:
        name = f"{name}:latest"
    role, tiers = scheduler.resolve_tiers(name, {})
    if role is None:
        return ollama_error(404, f"model '{name}' not found")
    denied = auth.client_allows(request, role, concrete=not _is_role(role))
    if denied:
        return ollama_error(403, denied)
    now = time.time()
    pick = scheduler.choose(role, tiers, None, now, mutate=False)
    if pick is not None:
        _i, tier, ctx, node = pick
        return web.json_response({"model": name, "available": True, "node": node.name, "served_by": tier["model"],
                                  "num_ctx": ctx, "warm": node.is_loaded(tier["model"])})
    why = scheduler.unavailable(tiers, None, now)
    return web.json_response({"model": name, "available": False, "code": why["code"], "detail": why["detail"],
                              "retry_after_s": why["retry_after_s"], "blockers": why["blockers"],
                              "wakeable": scheduler.wakeable_for(tiers) is not None})


async def handle_forbidden(request):
    return ollama_error(403, "model management is per node, not via router")


async def handle_root(request):
    return web.Response(text="Ollama is running (router)")
