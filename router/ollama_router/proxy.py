"""Die Ollama-API nach aussen: Anfragen annehmen, zuweisen, durchreichen.

Aufbau (Refactoring 2026-09-16): `route_request` -> `_route` bereitet die Anfrage vor (`_prepare`), waehlt und
reserviert einen Knoten (`_Acquire`) und laesst `dispatch` eine Stufe ausfuehren. `dispatch` ist ein duenner Einstieg
in `_Relay`, das je Anfrage den Zustand haelt (Zeiten, Tokens, Ergebnis) und die Wege Knoten-unary, Knoten-Stream und
Cloud in kleinen Methoden abbildet. Fehler vor dem ersten Byte liefern None -> `_route` nimmt den naechsten Kandidaten.
"""

import asyncio
import json
import time

from aiohttp import ClientError, ClientTimeout, web

from . import admission, auth, cloud, decision, metrics, nodes, ops, perf, poll, scheduler, state, toolcall_rescue, wol
from . import request as request_mod
from .common import VERSION, GIB, log, ollama_error, parse_keep_alive, read_json

STREAM_PATHS = ("/api/chat", "/api/generate")   # nur hier streamt Ollama zeilenweise NDJSON
CONNECT_TIMEOUT_S = 5                            # Verbindungsaufbau zum Knoten; die Antwort darf request_timeout_s dauern
NDJSON = "application/x-ndjson"
UPSTREAM_ERRORS = (ClientError, asyncio.TimeoutError, OSError)
CLIENT_FAULT_STATUSES = (400, 404, 413, 422)     # Cloud-Fehler, die ein anderer Knoten nicht besser kann -> an den Client


class _Reject(Exception):
    """Anfrage wird mit HTTP-Status und Klartext abgewiesen (im Format des Clients, siehe `err` in _route)."""

    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _is_role(role):
    """Rolle (standard:latest ...) oder konkretes Modell? Konkrete Modelle bekommen Kontext/Shadow/Canary nicht."""
    return role["exposed"] in state.CFG.roles


# --- Einstieg -------------------------------------------------------------------------------------------------------

async def handle_infer(request):
    body = await read_json(request)
    if body is None:
        return ollama_error(400, "invalid json")
    return await route_request(request, request.path, body)


async def route_request(request, path, body, shape=None, err=ollama_error):
    """Modell aufloesen (Rolle oder konkret), Knoten waehlen, Anfrage durchreichen. `shape` formt die Antwort
    (None = Ollama-Format 1:1, sonst z. B. OpenAI), `err` baut Fehlerantworten im Format des Clients.
    Stufe 6: mit Idempotency-Key (nur ohne Stream) liefert eine Wiederholung die gecachte Antwort."""
    key = ops.idem_key(request, body, path)
    if not key:
        return await _route(request, path, body, shape, err)
    cached = await ops.idem_wait(key)
    if cached is not None:
        return ops.idem_replay(cached)
    try:
        resp = await _route(request, path, body, shape, err)
    except BaseException:
        ops.idem_abort(key)
        raise
    ops.idem_finish(key, resp)
    return resp


async def _route(request, path, body, shape, err):
    try:
        name, role, tiers, req = await _prepare(request, path, body)
    except _Reject as e:
        return err(e.status, e.msg)
    req.priority = _priority_for(req, role, request)
    acquire = _Acquire(name, role, tiers, req, _client_num_ctx(body))
    while True:
        try:
            tier_idx, tier, ctx, node = await acquire.next_candidate()
        except _Reject as e:
            return err(e.status, e.msg)
        result = await dispatch(request, path, body, role, tier, ctx, node, tier_idx, shape, req)
        if result is not None:
            if _is_role(role):
                ops.maybe_shadow(role, body, node, req)   # Stufe 6: Schattenlauf nach der Antwort, Client merkt nichts
            return result
        # None = Fehler vor dem ersten Byte -> naechster Kandidat


# --- Vorbereitung: Name, routing-Block, Rolle, Stufen ----------------------------------------------------------------

async def _prepare(request, path, body):
    """Alles, was ohne Knoten entscheidbar ist. Liefert (name, role, tiers, req) oder wirft _Reject."""
    name = _requested_name(body)
    try:
        req = request_mod.parse(body, path, request.headers)   # Stufe 1: routing-Block, abgeleitete Anforderungen
    except ValueError as e:
        raise _Reject(400, str(e)) from None
    too_big = request_mod.check_limits(body)
    if too_big:
        raise _Reject(413, too_big)
    name = await _decide_role(request, path, body, name, req)
    role, tiers = scheduler.resolve_tiers(name, body)
    if role is None:
        raise _Reject(404, f"model '{name}' not found")
    decider = decision.active()
    if decider and req.decision is None:
        decider.record_client_choice(body, path, role["name"], request.get("client"), req.request_id)
    denied = auth.client_allows(request, role, concrete=not _is_role(role))
    if denied:
        raise _Reject(403, denied)
    tiers = _with_canary(role, tiers, req)
    tiers = _cloud_ordered(request, body, tiers, req)
    _ensure_satisfiable(name, role, tiers, req)
    return name, role, tiers, req


async def _decide_role(request, path, body, name, req):
    """Auto-Rolle: die Decision Engine waehlt eine der konfigurierten Rollen; alle anderen Namen gehen unveraendert durch."""
    decider = decision.active()
    if not decider or name != decider.exposed:
        return name
    result = await decider.decide_body(body, path, request.get("client"), req.request_id)
    req.decision = result.as_dict()
    exposed = decision.exposed_for(result.selected)
    if exposed is None:
        raise _Reject(503, f"decision engine chose unknown role {result.selected!r}")
    return exposed


def _requested_name(body):
    name = body.get("model") or body.get("name")
    if not name:
        raise _Reject(400, "model is required")
    if ":" not in name and name not in state.CFG.roles and f"{name}:latest" in state.CFG.roles:
        name = f"{name}:latest"
    return name


def _with_canary(role, tiers, req):
    """Stufe 6: ausgelost -> Canary-Stufe an die Spitze, Rest bleibt als Ausweich."""
    ct = ops.canary_tier(role) if _is_role(role) else None
    if not ct:
        return tiers
    req.canary = ct["model"]
    return [ct] + list(tiers)


def _cloud_ordered(request, body, tiers, req):
    """Stufe 5: Schranken fuer die Cloud-Stufen dieser Anfrage; execution=cloud zieht sie nach vorn."""
    if not any(t.get("cloud") for t in tiers):
        return tiers
    req.cloud_block = cloud.request_block_reason(req, body, request.get("client"))
    if req.execution == "cloud" and not req.cloud_block:
        return [t for t in tiers if t.get("cloud")] + [t for t in tiers if not t.get("cloud")]
    return tiers


def _ensure_satisfiable(name, role, tiers, req):
    """Keine Stufe kann, was der Request verlangt -> Fehler des Requests (400), kein Kapazitaetsproblem (503)."""
    blocked = [req.tier_blocked(t) for t in tiers]
    if not all(blocked):
        return
    reasons = "; ".join(sorted({f"{t['model']}: {b}" for t, b in zip(tiers, blocked, strict=True)}))
    state.remember({"event": "unsatisfiable", "role": role["name"], "request_id": req.request_id, "reasons": reasons})
    if _is_role(role):
        raise _Reject(400, f"no tier of '{name}' can serve this request ({reasons})")
    raise _Reject(400, f"model '{name}' cannot serve this request ({reasons})")


def _client_num_ctx(body):
    ctx = (body.get("options") or {}).get("num_ctx")
    return int(ctx) if ctx else None


def _priority_for(req, role, request):
    """Stufe 3: Prioritaetsklasse = routing.priority, sonst die der Rolle; die Client-Identitaet kappt nach oben."""
    prio = req.priority or role.get("priority") or "normal"
    client = (auth.client_auth_cfg().get("clients") or {}).get(request.get("client") or "") or {}
    cap = client.get("max_priority")
    if cap and admission.PRIORITIES[prio] < admission.PRIORITIES[cap]:
        return cap
    return prio


# --- Knoten waehlen und Platz bekommen (Scheduler + Admission + WOL) -------------------------------------------------

class _Acquire:
    """Kandidatenwahl je Versuch: Scheduler fragen, bei vollem Knoten in der Prioritaetsschlange warten, bei
    erschoepften Kandidaten einmal wecken. Merkt sich versuchte Knoten und die Wartezeit fuer routing.queued_ms."""

    def __init__(self, name, role, tiers, req, client_ctx):
        self.name, self.role, self.tiers, self.req, self.client_ctx = name, role, tiers, req, client_ctx
        self.tried = set()
        self.woke = False
        self.t_enq = None
        self.deadline_at = time.time() + (req.deadline_ms / 1000.0 if req.deadline_ms else state.CFG.admission["max_wait_s"])

    async def next_candidate(self):
        while True:
            now = time.time()
            pick = scheduler.choose(self.role, self.tiers, self.client_ctx, now, self.tried, self.req)
            if pick is None:
                await self._wake_or_reject()
                continue
            node = pick[3]
            if admission.saturated(node):
                await self._wait_for_slot(node, now)
                continue   # neu waehlen: Platz frei, Knoten weg oder ein anderer inzwischen besser
            if self.t_enq is not None:
                self.req.queued_ms = (time.time() - self.t_enq) * 1000
            self.tried.add(node.name)
            return pick

    async def _wake_or_reject(self):
        """Kandidaten erschoepft -> einmal Weckversuch, danach 503."""
        node = None if self.woke else scheduler.wakeable_for(self.tiers)
        if node is None:
            state.remember({"event": "no_node", "role": self.role["name"], "tried": sorted(self.tried)})
            raise _Reject(503, f"no node available for model '{self.name}'")
        self.woke = True
        if not await wol.wake(node):
            raise _Reject(503, f"no node available for model '{self.name}' (wake of {node.name} failed)")

    async def _wait_for_slot(self, node, now):
        """Stufe 3: Knoten voll -> hier warten (mit Prioritaet und Alterung), nicht in Ollamas Schlange ohne Prioritaet."""
        if self.t_enq is None:
            self.t_enq = now
            state.remember({"event": "queued", "role": self.role["name"], "node": node.name, "priority": self.req.priority,
                            "request_id": self.req.request_id, "inflight": node.inflight})
        if now >= self.deadline_at:
            waited = round((now - self.t_enq) * 1000)
            state.remember({"event": "admission_timeout", "role": self.role["name"], "node": node.name, "priority": self.req.priority,
                            "request_id": self.req.request_id, "waited_ms": waited})
            raise _Reject(503, f"queue deadline exceeded after {waited} ms (priority {self.req.priority}, node {node.name} "
                               f"has {node.inflight}/{node.effective_max_inflight()} requests running)")
        try:
            await admission.wait_for_slot(node, self.req.priority, self.t_enq, self.deadline_at, self.req.request_id, self.role["name"])
        except admission.QueueFull as e:
            raise _Reject(503, str(e)) from None


# --- Eine Stufe ausfuehren ---------------------------------------------------------------------------------------------

async def dispatch(request, path, body, role, tier, ctx, node, tier_idx, shape=None, req=None):
    """Eine Stufe auf einem Knoten oder Cloud-Anbieter ausfuehren. None = Fehler vor dem ersten Byte -> naechster Kandidat."""
    relay = _Relay(request, path, body, role, tier, ctx, node, tier_idx, shape, req or request_mod.Routing.fresh())
    return await relay.run()


def _backend_body(body, role, tier, ctx, node):
    """Body fuer den Knoten: konkretes Modell, Kontext, keep_alive nach Knotenzustand. Liefert (out, ctx)."""
    out = dict(body)
    out["model"] = tier["model"]
    opts = dict(out.get("options") or {})
    if _is_role(role) or "num_ctx" in opts:
        opts["num_ctx"] = ctx
    else:
        # konkretes Modell ohne Kontextangabe: den Kontext nehmen, mit dem es schon geladen ist,
        # sonst laedt Ollama mit seinem Default (OLLAMA_CONTEXT_LENGTH) neu -> 15-20 s und HA-Modell weg
        ctx = node.loaded_context(tier["model"]) or None
        if ctx:
            opts["num_ctx"] = ctx
    out["options"] = opts
    out["keep_alive"] = parse_keep_alive(state.CFG.keep_alive.get(node.state, "5m"))
    return out, ctx


def _failed_before_first_byte(status):
    """5xx (ausser 501) und 404 sind Knotenprobleme: naechster Kandidat. 4xx und 501 gehen an den Client."""
    return (status >= 500 and status != 501) or status == 404


class _Relay:
    """Zustand und Ablauf einer Zuweisung: Buchfuehrung am Knoten, Durchreichen, Tool-Call-Rettung, Metriken."""

    def __init__(self, request, path, body, role, tier, ctx, node, tier_idx, shape, req):
        self.request, self.path, self.role, self.tier, self.node = request, path, role, tier, node
        self.tier_idx, self.shape, self.req = tier_idx, shape, req
        self.model, self.exposed = tier["model"], role["exposed"]
        self.out, self.ctx = _backend_body(body, role, tier, ctx, node)
        self.stream = bool(self.out.get("stream", True)) and path in STREAM_PATHS
        # Ollama beschraenkt den tools-Pfad nicht und verwirft einen missglueckten Aufruf still;
        # gemessen verliert qwen3-coder:30b so 80 % seiner Calls oberhalb ~12k Token. Siehe toolcall_rescue.
        self.rescue = state.CFG.toolcall_rescue and toolcall_rescue.aktiv_fuer(self.out)
        self.is_cloud = getattr(node, "is_cloud", False)
        self.warm = node.is_loaded(self.model)
        self.t0 = time.time()
        self.outcome = None          # Stufe 3: ok | error | timeout | structured_error (perf_outcome + Breaker)
        self.ptoks = self.ctoks = 0  # Stufe 4: Tokens fuer Metriken/Usage
        self.ttft_s = None
        self.info = self.headers = None

    async def run(self):
        self._begin()
        try:
            if self.is_cloud:   # Stufe 5: Anbieter statt Knoten
                return await self._via_cloud()
            return await self._via_node()
        except (*UPSTREAM_ERRORS, ConnectionResetError) as e:
            # Fehler nach dem ersten Byte: nichts mehr zu retten, Verbindung endet
            log.warning("stream from %s aborted: %s", self.node.name, e)
            raise web.HTTPBadGateway(text=json.dumps({"error": f"upstream {self.node.name} aborted"})) from e
        finally:
            self._finish()

    # -- Buchfuehrung --

    def _begin(self):
        node, model = self.node, self.model
        node.inflight += 1
        node.inflight_models[model] += 1
        node.last_used[model] = self.t0   # fuer die Residenz-Regel: wird der Verdraenger noch gebraucht?
        if not self.warm:
            node.announce_load(model, self.ctx or self.tier["num_ctx"])
        via = self.shape.name if self.shape else "ollama"
        self.info = self.req.info(self.role, self.tier_idx, self.tier, self.ctx, node, self.warm, via, self.request.get("client"))
        self.headers = request_mod.headers_for(self.info)
        request_mod.remember_session(self.req.session_id, node.name, model)
        reason = "loaded" if self.warm else "cold"
        log.info("route %s %s -> tier%d %s ctx=%s node=%s(%s,%s) inflight=%d req=%s%s", self.path, self.role["name"], self.tier_idx,
                 model, self.ctx, node.name, node.state, reason, node.inflight, self.req.request_id,
                 f" skipped={len(self.req.skipped)}" if self.req.skipped else "")
        state.remember({"event": "route", "path": self.path, "role": self.role["name"], "tier": self.tier_idx, "model": model,
                        "ctx": self.ctx, "node": node.name, "node_state": node.state, "warm": self.warm, "via": via,
                        "request_id": self.req.request_id, "session_id": self.req.session_id, "client": self.request.get("client"),
                        "reason": self.req.reason, "skipped": len(self.req.skipped),
                        "canary": bool(self.req.canary) and model == self.req.canary})

    def _finish(self):
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
        admission.release(node)   # Stufe 3: Platz frei -> bestplatzierten Wartenden wecken
        log.info("done %s node=%s %.1fs%s", self.role["name"], node.name, elapsed, f" {cost:.4f} CHF" if cost else "")
        if not self.warm and not self.is_cloud:
            # frisch geladenes Modell sofort registrieren, nicht erst beim naechsten 5-s-Poll:
            # sonst gilt die naechste Anfrage bis zu 5 s lang faelschlich als Kaltstart
            asyncio.create_task(poll.poll_node(node))

    def _fail(self, outcome, breaker_reason):
        """Fehler vor dem ersten Byte: Breaker zaehlt, Aufrufer nimmt den naechsten Kandidaten."""
        self.outcome = outcome
        self.node.breaker_fail(time.time(), breaker_reason)
        return None

    # -- Knoten --

    async def _via_node(self):
        timeout = ClientTimeout(total=state.CFG.request_timeout_s, sock_connect=CONNECT_TIMEOUT_S, sock_read=state.CFG.request_timeout_s)
        try:
            upstream = await nodes.nreq(self.node, "post", self.path, json=self.out, timeout=timeout)
        except UPSTREAM_ERRORS as e:
            log.warning("node %s connect failed: %s", self.node.name, e)
            return self._fail("timeout" if isinstance(e, asyncio.TimeoutError) else "error", f"connect: {type(e).__name__}")
        async with upstream:
            if _failed_before_first_byte(upstream.status):
                return await self._node_failed(upstream)
            if upstream.status >= 400:
                return await self._pass_through_error(upstream)
            if not self.stream:
                return await self._complete_unary(upstream)
            return await self._complete_stream(upstream)

    async def _node_failed(self, upstream):
        txt = await upstream.text()
        log.warning("node %s returned %s before first byte: %s", self.node.name, upstream.status, txt[:200])
        if upstream.status == 404:
            self.node.models.discard(self.model)   # Knoten hat das Modell nicht (mehr); kein Breaker-Fall
            return None
        return self._fail("structured_error" if self.out.get("format") else "error", f"HTTP {upstream.status}")

    async def _pass_through_error(self, upstream):
        """4xx und 501 ("does not support embeddings": Modell kann das nicht, ein anderer Knoten auch nicht) -> an den Client."""
        txt = await upstream.read()
        if not self.shape:
            return web.Response(status=upstream.status, body=txt, content_type="application/json", headers=self.headers)
        try:
            msg = json.loads(txt).get("error") or txt.decode(errors="replace")
        except ValueError:
            msg = txt.decode(errors="replace")
        return self.shape.error(upstream.status, str(msg))

    async def _complete_unary(self, upstream):
        data = await upstream.read()
        try:
            j = json.loads(data)
        except ValueError:
            if self.shape:
                return self.shape.error(502, f"upstream {self.node.name}: invalid json")
        else:
            if isinstance(j, dict):
                if self.rescue:
                    self._rescue_unary(j)
                self._record_stats(j, ttft=None)
                data = self._finalize_json(j)
            else:
                data = self.shape.complete(j) if self.shape else json.dumps(j).encode()
        self.outcome = "ok"
        return web.Response(status=200, body=data, content_type="application/json", headers=self.headers)

    async def _complete_stream(self, upstream):
        resp = await self._open_stream()
        rescuer = toolcall_rescue.StreamRescuer(self.out.get("tools")) if self.rescue else None
        first_at = None
        async for line in upstream.content:
            if not line.strip():
                continue
            first_at = first_at or time.time()
            chunk = self._render_chunks(line, rescuer, first_at)
            if chunk is None:            # kein JSON: im Ollama-Format roh weiterreichen, im Fremdformat weglassen
                chunk = b"" if self.shape else line
            if chunk:
                await resp.write(chunk)
        self._note_stream_rescue(rescuer)
        if self.shape:
            await resp.write(self.shape.tail())
        await resp.write_eof()
        self.outcome = "ok"
        return resp

    def _render_chunks(self, line, rescuer, first_at):
        """Einen NDJSON-Chunk in das Client-Format bringen. None = kein JSON. Der Retter kann einen Chunk zurueckhalten
        (leere Liste) oder in zwei aufspalten (gehaltener Text + Rest) - darum die Schleife; ohne Rettung ein Durchlauf."""
        try:
            j = json.loads(line)
        except ValueError:
            return None
        out = b""
        for jj in (rescuer.chunk(j) if rescuer else [j]):
            if "model" in jj:
                jj["model"] = self.exposed
            if jj.get("done"):
                self._record_stats(jj, ttft=first_at - self.t0 - (jj.get("load_duration") or 0) / 1e9)
                if self.req.explicit:
                    jj["routing"] = self.info
            out += (self.shape.chunk(jj) if self.shape else (json.dumps(jj) + "\n").encode()) or b""
        return out

    # -- Cloud (Stufe 5) --

    async def _via_cloud(self):
        """Anfrage an einen Cloud-Anbieter (cloud.run), Antwort im Ollama-Format an den Client. None = naechster Kandidat
        (Anbieterfehler vor dem ersten Byte, Breaker zaehlt mit)."""
        try:
            cinfo, result = await cloud.run(self.node, self.path, self.out, self.model, self.stream)
        except cloud.CloudError as e:
            log.warning("%s: %s -> HTTP %s: %s", self.node.name, self.model, e.status, e)
            self._fail("timeout" if e.status == 408 else "error", f"HTTP {e.status}")
            if e.status in CLIENT_FAULT_STATUSES and not str(e).lower().startswith("the model"):
                return self._client_error(e.status, str(e))
            return None
        except UPSTREAM_ERRORS as e:
            log.warning("%s connect failed: %s", self.node.name, e)
            return self._fail("timeout" if isinstance(e, asyncio.TimeoutError) else "error", f"connect: {type(e).__name__}")
        if not self.stream:
            self.ptoks, self.ctoks = cinfo["p_tok"], cinfo["c_tok"]
            self.outcome = "ok"
            return web.Response(status=200, body=self._finalize_json(result, force_model=True), content_type="application/json", headers=self.headers)
        return await self._cloud_stream(result, cinfo)

    async def _cloud_stream(self, chunks, cinfo):
        """cinfo fuellt cloud.run erst waehrend des Streams (usage kommt am Ende) - Tokens darum erst nach write_eof lesen."""
        resp = None
        try:
            async for j in chunks:
                if resp is None:
                    resp = await self._open_stream()
                j["model"] = self.exposed
                if j.get("done") and self.req.explicit:
                    j["routing"] = self.info
                line = self.shape.chunk(j) if self.shape else (json.dumps(j) + "\n").encode()
                if line:
                    await resp.write(line)
        except cloud.CloudError as e:
            if resp is None:   # noch nichts gesendet -> naechster Kandidat
                log.warning("%s: %s -> HTTP %s: %s", self.node.name, self.model, e.status, e)
                return self._fail("error", f"HTTP {e.status}")
            raise web.HTTPBadGateway(text=json.dumps({"error": f"{self.node.name} aborted: {e}"})) from e
        if resp is None:   # leerer Stream: trotzdem sauber abschliessen
            resp = await self._open_stream()
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
        resp = web.StreamResponse(status=200, headers={"Content-Type": self.shape.stream_content_type if self.shape else NDJSON, **self.headers})
        await resp.prepare(self.request)
        return resp

    def _finalize_json(self, j, force_model=False):
        """model-Feld auf den Namen, den der Client kennt; routing-Block nur bei explizitem routing-Block im Request."""
        if force_model or "model" in j:
            j["model"] = self.exposed
        if self.req.explicit:
            j["routing"] = self.info
        return self.shape.complete(j) if self.shape else json.dumps(j).encode()

    def _record_stats(self, j, ttft):
        """Ollamas Zaehler (eval_count ...) fuer Tempo-Statistik und Token-Metriken uebernehmen."""
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
        elif not jm.get("tool_calls") and toolcall_rescue.verdacht(jm.get("content")):
            self._note_lost(stream=False, content=jm.get("content"))

    def _note_stream_rescue(self, rescuer):
        if rescuer is None:
            return
        if rescuer.gerettet:
            self._note_rescue(rescuer.gerettet, stream=True)
        elif rescuer.verloren:
            self._note_lost(stream=True)

    def _note_rescue(self, tool, stream):
        log.warning("tool-call gerettet%s: %s hatte %s als Text geschrieben (req=%s)", " (stream)" if stream else "",
                    self.model, tool, self.req.request_id)
        state.remember({"event": "toolcall_rescue", "node": self.node.name, "model": self.model, "tool": tool,
                        "stream": stream, "request_id": self.req.request_id})

    def _note_lost(self, stream, content=None):
        """Unbekannter Dialekt: fuer den Client verloren, aber nicht mehr stumm."""
        sample = f": {(content or '').replace(chr(10), ' ')[:120]}" if content else ""
        log.warning("tool-call VERLOREN (Dialekt unbekannt%s): %s req=%s%s", ", stream" if stream else "", self.model,
                    self.req.request_id, sample)
        state.remember({"event": "toolcall_lost", "node": self.node.name, "model": self.model, "stream": stream,
                        "request_id": self.req.request_id})


# --- Auskunft: tags, show, ps, version -------------------------------------------------------------------------------

async def handle_tags(request):
    return web.json_response({"models": tags_list()})


def tags_list():
    """Modellliste wie /api/tags: Rollen (mit den Details ihres Rang-1-Modells) + ggf. konkrete Modelle aller Knoten."""
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
    if decider:   # die Auto-Rolle ist fuer Clients ein Modell wie jede Rolle
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
    if decider and name in (decider.exposed, decider.role):   # /show der Auto-Rolle: Auskunft ueber die Default-Rolle
        name = decision.exposed_for(decider.default) or name
    role, tiers = scheduler.resolve_tiers(name, body)
    if role is None:
        return ollama_error(404, f"model '{name}' not found")
    model = tiers[0]["model"]
    if tiers[0].get("cloud"):   # Stufe 5: Cloud-Modell hat kein Ollama - synthetische Auskunft
        return web.json_response({"capabilities": request_mod.effective_caps(model) or ["completion"],
                                  "details": {"family": "cloud", "provider": tiers[0]["cloud"]},
                                  "model_info": {}, "modelfile": "", "parameters": ""})
    n = scheduler.any_online_node_with(model)
    if n is None:
        # letzter Ausweg: Knoten, der das Modell zuletzt hatte, wecken lohnt fuer /show nicht
        return ollama_error(503, f"no online node holds '{model}'")
    try:
        async with nodes.nreq(n, "post", "/api/show", json={"model": model, "verbose": bool(body.get("verbose", False))},
                              timeout=ClientTimeout(total=15)) as r:
            data = await r.read()
            return web.Response(status=r.status, body=data, content_type="application/json")
    except UPSTREAM_ERRORS as e:
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


async def handle_forbidden(request):
    return ollama_error(403, "model management is per node, not via router")


async def handle_root(request):
    return web.Response(text="Ollama is running (router)")
