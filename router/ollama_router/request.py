"""Stufe 1 (design/roadmap.md): Request-Semantik - der Client beschreibt, der Router entscheidet.

Ein Request traegt optional einen `routing`-Block, in derselben Form nativ (/api/chat, /api/generate, /api/embed) und
OpenAI-kompatibel (/v1/*). Er wird NICHT an Ollama weitergereicht.

    routing:
      require: [tools, vision, thinking, structured, embedding, insert]  # Pflicht: Stufen ohne diese Faehigkeit fallen weg
      prefer:  [thinking]        # Wunsch: kann eine Stufe damit bedient werden, kommen nur solche in Frage
      min_context: 32768         # Untergrenze fuer den Kontext der Stufe (die Kontextleiter bleibt der Fit-Mechanismus)
      session_id: "..."          # Affinitaet: gleicher Knoten + gleiches Modell wie zuletzt, solange es dort warm ist
      request_id: "..."          # gespiegelt in Antwort (routing-Block), Header X-Skirnir-Request-Id und Entscheidungsprotokoll

Anforderungen leitet der Router auch aus dem Request selbst ab: tools -> tools, Bilder -> vision, think -> thinking,
format als Schema/json -> structured, suffix -> insert (embedding nur auf ausdrueckliches require). Faehigkeiten stammen aus Ollamas
/api/show (state.CAPS), ergaenzt um Katalog-Overrides `models.<name>.capabilities: {structured: false, ...}` - Ollama
kennt `structured` nicht als Faehigkeit, gpt-oss antwortet auf ein Schema aber mit HTTP 500.

Antwort: Header X-Skirnir-Request-Id/-Node/-Model/-Tier/-Warm immer; der `routing`-Block im Body nur, wenn der Client
selbst einen `routing`-Block geschickt hat (Altclients wie die HA-Ollama-Integration sehen einen unveraenderten Body).
"""

import secrets
import time

from . import cloud, state
from .common import DATA_CLASSES_DEFAULT, PRIORITIES

KNOWN_CAPS = ("completion", "tools", "vision", "thinking", "structured", "embedding", "insert")
# Ollama meldet diese nicht als Faehigkeit; sie gelten, solange der Katalog nichts anderes sagt
DEFAULT_TRUE = {"structured", "completion"}
ROUTING_KEYS = {"require", "prefer", "min_context", "session_id", "request_id", "priority", "deadline_ms", "idempotency_key",
                "execution", "data_class"}
EXECUTIONS = ("auto", "local", "cloud")   # Stufe 5: auto = Rollenreihenfolge, local = Opt-out, cloud = Cloud-Stufen zuerst
MAX_ID_LEN = 128
REQUEST_ID_BYTES = 6   # "skirnir-" + 12 Hex-Zeichen: eindeutig genug fuer Log und Entscheidungsprotokoll, kurz genug zum Abtippen


def new_request_id():
    return "skirnir-" + secrets.token_hex(REQUEST_ID_BYTES)


def overrides(model):
    twin = state.CFG.catalog_twin(model) or {}
    return twin.get("capabilities") or {}


def lacks(model, caps):
    """Welche der verlangten Faehigkeiten fehlen dem Modell nachweislich? Unbekannt (noch kein /api/show) blockiert nicht -
    der Router sperrt nur, was er weiss; Ollama meldet den Rest selbst."""
    raw = state.CAPS.get(model)
    over = overrides(model)
    missing = []
    for c in caps:
        if c in over:
            if not over[c]:
                missing.append(c)
            continue
        if c in DEFAULT_TRUE:
            continue
        if raw is None:
            continue
        if c not in raw:
            missing.append(c)
    return missing


def effective_caps(model):
    """Fuer /admin/state: gemeldete Faehigkeiten + Katalog-Overrides; None solange nichts bekannt ist."""
    raw = state.CAPS.get(model)
    over = overrides(model)
    if raw is None and not over:
        return None
    caps = set(raw or []) | DEFAULT_TRUE
    for c, v in over.items():
        (caps.add if v else caps.discard)(c)
    return sorted(caps)


def implicit_caps(body, path):
    caps = set()
    # /api/embed leitet KEINE Anforderung 'embedding' ab: Ollama fuehrt die Faehigkeit nur bei reinen Embedding-Modellen,
    # Chat-Modelle koennen trotzdem einbetten (und die, die nicht koennen, melden 501 - das reicht der Router 1:1 durch).
    if body.get("tools"):
        caps.add("tools")
    msgs = body.get("messages") or []
    if body.get("images") or any(isinstance(m, dict) and m.get("images") for m in msgs):
        caps.add("vision")
    think = body.get("think")
    if think is True or (isinstance(think, str) and think.lower() not in ("", "false", "none", "off")):
        caps.add("thinking")
    fmt = body.get("format")
    if isinstance(fmt, dict) or fmt == "json":
        caps.add("structured")
    if body.get("suffix"):
        caps.add("insert")
    return caps


def count_images(body):
    n = len(body.get("images") or [])
    for m in body.get("messages") or []:
        if isinstance(m, dict):
            n += len(m.get("images") or [])
    return n


class Routing:
    @classmethod
    def fresh(cls):
        """Leere Routing-Angaben mit neuer Request-Id (interne Anfragen ohne Client-Body, z. B. Schattenlauf)."""
        r = cls()
        r.request_id = new_request_id()
        return r

    def __init__(self):
        self.require = set()
        self.prefer = set()
        self.min_context = None
        self.session_id = None
        self.request_id = None
        self.explicit = False      # Client hat einen routing-Block geschickt -> Routing-Info in den Body
        self.skipped = []          # [{"tier", "model", "ctx", "reason"}] der letzten Auswahl
        self.reason = None         # affinity | warm-first | rank
        self.candidates = []       # Stufe 3: Kandidaten der gewaehlten Stufe mit Score
        self.priority = None       # Stufe 3: interactive | normal | batch (Rolle als Default, Client kappt)
        self.deadline_ms = None    # Stufe 3: laengstes Warten auf einen Platz
        self.queued_ms = 0
        self.canary = None         # Stufe 6: Modell der Canary-Stufe, wenn diese Anfrage ausgelost wurde
        self.execution = "auto"    # Stufe 5
        self.data_class = None
        self.data_class_effective = None
        self.cloud_block = None    # Grund, warum diese Anfrage nicht in die Cloud darf (gilt fuer alle Cloud-Stufen)
        self.decision = None       # Decision Engine: Ergebnis (as_dict), wenn die Auto-Rolle angefragt wurde

    def tier_blocked(self, tier):
        """Grund, warum diese Stufe fuer den Request nicht in Frage kommt - oder None."""
        if tier.get("cloud"):
            why = self.cloud_block or cloud.provider_block_reason(tier, self)
            if why:
                return why
        if self.min_context and tier["num_ctx"] < self.min_context:
            return f"num_ctx {tier['num_ctx']} < min_context {self.min_context}"
        missing = lacks(tier["model"], sorted(self.require))
        if missing:
            return "fehlende Faehigkeit: " + ", ".join(missing)
        return None

    def info(self, role, tier_idx, tier, ctx, node, warm, via, client=None):
        d = {"request_id": self.request_id, "role": role["name"], "model": tier["model"], "node": node.name,
             "tier": tier_idx, "ctx": ctx, "warm": warm, "via": via, "reason": self.reason,
             "node_state": node.state, "skipped": list(self.skipped)}
        if self.session_id:
            d["session_id"] = self.session_id
        if client:
            d["client"] = client
        if self.require:
            d["required"] = sorted(self.require)
        d["candidates"] = list(self.candidates)
        d["priority"] = self.priority
        d["queued_ms"] = round(self.queued_ms)
        d["canary"] = bool(self.canary) and tier["model"] == self.canary
        if self.decision:
            d["decision"] = self.decision
        d["cloud"] = bool(tier.get("cloud"))
        d["execution"] = self.execution
        if self.data_class_effective:
            d["data_class"] = self.data_class_effective
        return d


def parse(body, path, headers=None):
    """routing-Block aus dem Body nehmen und pruefen (ValueError mit Klartext), Anforderungen ableiten."""
    rb = body.pop("routing", None)
    r = Routing()
    if rb is not None:
        if not isinstance(rb, dict):
            raise ValueError("routing must be an object")
        unknown = set(rb) - ROUTING_KEYS
        if unknown:
            raise ValueError(f"routing: unknown field(s) {', '.join(sorted(unknown))}; known: {', '.join(sorted(ROUTING_KEYS))}")
        for key in ("require", "prefer"):
            vals = rb.get(key) or []
            if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
                raise ValueError(f"routing.{key} must be a list of capability names")
            bad = [v for v in vals if v not in KNOWN_CAPS]
            if bad:
                raise ValueError(f"routing.{key}: unknown capability {', '.join(bad)}; known: {', '.join(KNOWN_CAPS)}")
            setattr(r, key, set(vals))
        mc = rb.get("min_context")
        if mc is not None:
            if not isinstance(mc, int) or isinstance(mc, bool) or mc <= 0:
                raise ValueError("routing.min_context must be a positive integer")
            r.min_context = mc
        for key in ("session_id", "request_id"):
            v = rb.get(key)
            if v is not None:
                v = str(v)
                if not v or len(v) > MAX_ID_LEN:
                    raise ValueError(f"routing.{key} must be 1-{MAX_ID_LEN} characters")
                setattr(r, key, v)
        pr = rb.get("priority")
        if pr is not None:
            if pr not in PRIORITIES:
                raise ValueError(f"routing.priority must be one of {', '.join(PRIORITIES)}")
            r.priority = pr
        dl = rb.get("deadline_ms")
        if dl is not None:
            if not isinstance(dl, int) or isinstance(dl, bool) or dl <= 0:
                raise ValueError("routing.deadline_ms must be a positive integer")
            r.deadline_ms = dl
        ex = rb.get("execution")
        if ex is not None:
            if ex not in EXECUTIONS:
                raise ValueError(f"routing.execution must be one of {', '.join(EXECUTIONS)}")
            r.execution = ex
        dc = rb.get("data_class")
        if dc is not None:
            classes = state.CFG.cloud.get("data_classes") or DATA_CLASSES_DEFAULT
            if dc not in classes:
                raise ValueError(f"routing.data_class must be one of {', '.join(classes)}")
            r.data_class = dc
        r.explicit = True
    r.require |= implicit_caps(body, path)
    if not r.request_id:
        hdr = (headers or {}).get("X-Request-Id") if headers is not None else None
        r.request_id = str(hdr)[:MAX_ID_LEN] if hdr else new_request_id()
    return r


def check_limits(body):
    """Request-Groessenlimits (router.limits) - 413-Klartext oder None. Grob, aber vor dem Backend."""
    lim = state.CFG.limits
    n = count_images(body)
    if n > lim["max_images"]:
        return f"too many images ({n} > {lim['max_images']})"
    n = len(body.get("tools") or [])
    if n > lim["max_tools"]:
        return f"too many tools ({n} > {lim['max_tools']})"
    n = len(body.get("messages") or [])
    if n > lim["max_messages"]:
        return f"too many messages ({n} > {lim['max_messages']})"
    return None


def headers_for(info):
    h = {"X-Skirnir-Request-Id": str(info["request_id"]), "X-Skirnir-Node": info["node"], "X-Skirnir-Model": info["model"],
         "X-Skirnir-Tier": str(info["tier"]), "X-Skirnir-Warm": "1" if info["warm"] else "0"}
    return h


# --- Session-Affinitaet (klein, §10): gleicher Knoten + gleiches Modell, solange dort warm; Eintraege verfallen ---

def affinity(session_id):
    e = state.SESSIONS.get(session_id)
    if not e:
        return None
    if time.time() - e["t"] > state.CFG.session_ttl_s:
        state.SESSIONS.pop(session_id, None)
        return None
    return e


def remember_session(session_id, node_name, model):
    if not session_id:
        return
    now = time.time()
    state.SESSIONS[session_id] = {"node": node_name, "model": model, "t": now}
    if len(state.SESSIONS) > 5000:   # Aufraeumen: abgelaufene raus, im Zweifel die aeltesten
        for k in sorted(state.SESSIONS, key=lambda k: state.SESSIONS[k]["t"])[:1000]:
            state.SESSIONS.pop(k, None)
