"""A GPU node: state, VRAM accounting, baseline, and `nreq` as the sole path to its Ollama."""

import asyncio
import time
from collections import Counter

from aiohttp import ClientError, Fingerprint

from . import config, state
from .common import log, tls_fingerprint

PUSH_STALE_S = 45   # Agent >= 0.14.0 reports Ollama status on every change and at least every 30 s
# Slowness watch (router 0.9.2): minimum sizes for a request's rate to reach HA. Below that the rate is mostly noise
# (a 2-token reply measured 7 tok/s on a model that generates 100; a cached prompt has only a few new tokens).
PERF_MIN_GEN_TOKENS = 20
PERF_MIN_PROMPT_TOKENS = 200
RAM_SPILL_MIN_GIB = 0.05   # below this, size - size_vram is rounding, not layers in system RAM

# GPU guard: Agents before 0.20.0 report states, reasons, and field names in German (design/english-names.md). guard_english
# translates on receipt so the router only sees English names; remove once all nodes are >= 0.20.0.
GUARD_STATES = ("off", "unavailable", "normal", "high_load", "throttled", "recovery")
GUARD_STATE_BEFORE_0_20 = {"aus": "off", "unverfuegbar": "unavailable", "hochlast": "high_load", "gedrosselt": "throttled",
                           "erholung": "recovery"}
GUARD_FIELD_BEFORE_0_20 = {"quelle": "source", "grund": "reason", "warnungen": "warnings", "hochlast_s": "high_load_s", "zeit": "at"}
GUARD_REASON_BEFORE_0_20 = {"abgewaehlt": "deselected"}


def guard_english(g):
    """GPU guard status of an agent with English field names and values (agents before 0.20.0 send German)."""
    out = {GUARD_FIELD_BEFORE_0_20.get(k, k): v for k, v in g.items()}
    out["state"] = GUARD_STATE_BEFORE_0_20.get(out.get("state"), out.get("state"))
    out["reason"] = GUARD_REASON_BEFORE_0_20.get(out.get("reason"), out.get("reason"))
    return out


class NodeNotReady(ClientError):
    """TLS node whose fingerprint is not yet known (no heartbeat so far)."""


def nreq(node, method, path, **kw):
    """HTTP request to a node: via the agent's tunnel if active; otherwise directly to the ollama-URL
    (TLS nodes: pinned certificate + router token). Nodes without URL and tunnel are considered unreachable."""
    if node.tunnel is not None:
        return node.tunnel.request(method, path, **kw)
    if not node.url:
        # exception-ok: nreq stands in for SESSION.request; callers handle an unreachable node as a ClientError at their network boundary
        raise NodeNotReady(f"{node.name}: kein Tunnel verbunden")
    if node.tls:
        if node.ssl is None:
            # exception-ok: nreq stands in for SESSION.request; callers handle an unreachable node as a ClientError at their network boundary
            raise NodeNotReady(f"{node.name}: TLS-Fingerprint unbekannt, warte auf Heartbeat")
        kw["ssl"] = node.ssl
        h = dict(kw.get("headers") or {})
        if state.CFG.heartbeat_token:   # Router token for the agent's TLS front-end (ollama_proxy, router.token)
            h["X-Router-Token"] = state.CFG.heartbeat_token
        kw["headers"] = h
    return state.SESSION.request(method, node.url + path, **kw)


class Breaker:
    """Circuit breaker per target (stage 3): closed -> open after `failures` errors in window -> after open_s half_open with exactly
    one probe. Shared for Node and CloudTarget (both carry name, inflight, breaker, breaker_fails, breaker_until)."""

    def breaker_would_allow(self, now):
        """Pure view without state change (HA view, /metrics): is a request allowed now?"""
        if self.breaker == "closed":
            return True
        if self.breaker == "open" and now < self.breaker_until:
            return False
        return self.inflight == 0

    def breaker_allows(self, now):
        if self.breaker == "open" and now >= self.breaker_until:
            self.breaker = "half_open"
            log.info("%s: breaker half_open - one probe", self.name)
            state.remember({"event": "breaker", "node": self.name, "state": "half_open"})
        return self.breaker_would_allow(now)

    def breaker_fail(self, now, why):
        cfg = state.CFG.breaker
        self.breaker_fails = [t for t in self.breaker_fails if now - t <= cfg["window_s"]] + [now]
        if self.breaker == "half_open" or len(self.breaker_fails) >= cfg["failures"]:
            self.breaker, self.breaker_until, self.breaker_fails = "open", now + cfg["open_s"], []
            log.warning("%s: breaker OPEN for %.0fs (%s)", self.name, cfg["open_s"], why)
            state.remember({"event": "breaker", "node": self.name, "state": "open", "reason": why})

    def breaker_ok(self):
        if self.breaker != "closed":
            self.breaker = "closed"
            log.info("%s: breaker closed (probe succeeded)", self.name)
            state.remember({"event": "breaker", "node": self.name, "state": "closed"})
        self.breaker_fails = []

    def breaker_reset(self):
        self.breaker, self.breaker_fails, self.breaker_until = "closed", [], 0.0


openai_node_serving_ollama = "ollama"   # OLLAMA frame serving (agent tunnel.upstream_fallback); openai_node.SERVING_OLLAMA


class Node(Breaker):
    is_cloud = False   # CloudTarget (cloud.py) returns True; Proxy/Ops distinguish by this, no getattr needed

    def set_fingerprint(self, hexfp):
        """Adopt TLS fingerprint. False if not a SHA-256 fingerprint (caller reports it)."""
        fp = tls_fingerprint(hexfp)
        if fp is None:
            return False
        self.tls_fp = fp
        self.ssl = Fingerprint(bytes.fromhex(fp))
        return True

    def __init__(self, name, spec):
        self.name = name
        self.url = (spec.get("ollama") or "").rstrip("/")   # empty = node reachable only via tunnel (agent connects)
        self.tunnel = None
        # TLS node (agent front-end before Ollama): certificate pinned via SHA-256 fingerprint, no CA.
        # "auto": agent reports its certificate fingerprint in heartbeat (secured by token + LE-TLS).
        self.tls = self.url.startswith("https://")
        self.ssl = None
        self.tls_fp = ""
        self.tls_mode = "auto"
        fp = spec.get("ollama_tls_fingerprint")
        if spec.get("ollama_tls_cert"):
            fp = config.cert_fingerprint(spec["ollama_tls_cert"])
        if fp:
            self.tls_mode = "fixed"
            if not self.set_fingerprint(fp):   # config.yaml validates format on load
                log.error("node %s: invalid TLS fingerprint: %r - node stays unreachable", name, fp)
        self.vram_total_gib = float(spec.get("vram_total_gib", 0))
        self.mac = spec.get("mac")
        self.wol = bool(spec.get("wol", False))
        self.weight = int(spec.get("weight", 1))
        self.foreign_baseline_gib = float(spec.get("foreign_vram_baseline_gib", 0))
        self.fp = None      # Fingerprint of the agent key (registered nodes)
        self.polled_ok = 0.0   # last successful poll (/api/ps known) - prerequisite for baseline learning
        self.ollama_push_ts = 0.0     # last Ollama status report by agent (frame OLLAMA); fresh = no poll
        self.ollama_push_error = None  # Reason agent reports for "Ollama not responding"
        self.reg = None     # Eintrag im Knotenregister (nodes.json)
        self.gpu = spec.get("gpu", "")
        # The agent fronts an OpenAI-compatible server instead of Ollama (agent fact upstream_api, openai_node.py)
        self.openai_upstream = spec.get("upstream_api") == "openai"
        # ... and can stop/start it on order to free the GPU (agent fact upstream_release, agent >= 0.22.0)
        self.upstream_releasable = spec.get("upstream_releasable") is True
        # ... and runs a standby Ollama while it is held (agent fact upstream_fallback, agent >= 0.23.0)
        self.upstream_fallback = spec.get("upstream_fallback") is True
        self.upstream_serving = ""    # agent report: "ollama" = the standby Ollama serves in place of the held server
        self.upstream_state = ""      # agent report: "held" (stopped for the GPU), "starting", "" = answering
        self.upstream_order = ""      # last order sent: "hold" / "release" (openai_node.order)
        # Ollama-Sicht
        self.models = set()
        self.last_known_models = set()
        self.model_details = {}          # name -> dict from /api/tags
        self.digest_of = {}              # name -> digest (alias and original share digest)
        self.loaded = {}                 # loaded name -> size_vram GiB (as reported by Ollama)
        self.loaded_digest = {}          # digest -> size_vram GiB
        self.loaded_ctx = {}             # digest -> context_length, with which the model is loaded
        self.ram_spill = {}              # loaded name -> GiB Ollama placed in system RAM (size - size_vram): runs partly on the CPU
        self.last_gen = None             # last warm request with enough output tokens: {"model", "tps", "ts"} (HA sensor)
        self.last_prompt = None          # last warm request with a long enough prompt: {"model", "tps", "ts"} (HA sensor)
        self.misses = 0
        self.state = "offline"
        # Agent-Sicht
        self.hb_ts = 0.0
        self.gpu_util = None
        self.sensors = None   # Extra sensors from heartbeat (Agent >= 0.6.0), dict or None
        self.guard = None     # GPU-Schutz laut Agent (>= 0.7.0): state, limit_w, target_w, problem, reason, warnings (guard_english)
        self.draining_until = 0.0   # Agent update running: no new requests to this node until done
        self.guard_ts = 0.0
        self.vram_free_gib = None
        self.vram_used_gib = None
        self.vram_total_reported_gib = None
        self.ollama_proc_gib = None
        self.children_vram_gib = 0.0   # Agent >= 0.13.0: Grafikspeicher seiner Kinder ausser Ollama (STT per CUDA), gemeldet
        # Agent >= 0.17.0: Game and RAM as fact (poll.apply_heartbeat). game None = agent does not report it
        # (too old / Linux without detection) - then only foreign VRAM and GPU load apply as before.
        self.game = None              # {"running", "name", "via", ...} from agent
        self.game_since = None        # since when router has seen the game continuously (busy only after game_sustain_s)
        # 0.8.7: does the running game need GPU? Once yes, stays yes until game ends (no flicker in menu/loading screen).
        # Without GPU requirement in report (Agent < 0.19.0, measurement failed), every game is heavy - as until 0.8.6.
        self.game_heavy = False
        self.game_heavy_why = ""      # Reason as fact for /admin/state and journal
        self.prewarm_after_game = False   # 0.8.8: Prewarm skipped due to game -> catch up when light/done
        # 0.14.0: sleep as a fact from the agent (frame POWER, Agent >= 0.24.0). None = never reported (older agent,
        # Linux, console mode); "asleep" from the suspend notice until the next resume notice or heartbeat.
        self.power = None             # "asleep" | "awake" | None
        self.power_since = None       # epoch seconds of the last change
        self.memory = None            # {"ram_available_gib", "commit_free_gib", ...} from agent
        self.routes = {}              # Agent tunnel routes besides Ollama: {"rerank": {"up": bool, "reason": str}}; empty = not reported
        self.mem_low_count = 0        # so many consecutive heartbeats below threshold
        self.mem_pressure = False     # Memory pressure: no cold loads, no prewarm (loaded ones keep responding)
        self.vram_last_gib = 0.0      # Ollama usage at last poll, to detect shrinkage
        self.vram_hold_gib = 0.0      # solange nvidia-smi nachzieht, gilt dieser Wert als Ollama-Belegung
        self.vram_hold_until = 0.0
        self.loading = {}             # Model -> (expected usage GiB, deadline): load triggered by router
        # Zustandsautomat
        self.hot_since = None
        self.foreign_since = None
        self.last_busy_unload = 0.0
        self.calm_since = None
        self.busy_reason = ""
        # Laufende Arbeit
        self.inflight = 0
        self.inflight_models = Counter()
        self.last_used = {}           # Model -> timestamp of last request via router (residency rule)
        self.last_residency = 0.0
        self.free_since = 0.0         # how long continuously free (driven by poll.residency_check)
        self.free_at = 0.0            # Timestamp of last switch to free; delayed prewarm applies only to "its" switch
        self.swap_pending = None      # Ollama version whose swap the router is currently releasing (ollamaupdate.release_swap)
        # Stage 3: Circuit Breaker (closed -> open after `failures` backend errors in window -> half_open after open_s:
        # one probe, success = closed, error = open again) and admission (max_inflight from policy, else default)
        self.breaker = "closed"
        self.breaker_fails = []
        self.breaker_until = 0.0
        self.max_inflight = int(spec["max_inflight"]) if spec.get("max_inflight") else None
        # Busy thresholds per node (policy in register, 2026-09-11): a 16-GiB card where Ollama holds 8 GiB leaves
        # a game never 6 GiB of "foreign" VRAM - gpu-laptop stayed free while gaming (util 35-40 %, foreign 3.9 GiB) and gemma4 stayed
        # loaded. None = global values from modes.busy_enter.
        self.busy_util_pct = float(spec["busy_gpu_util_pct"]) if spec.get("busy_gpu_util_pct") is not None else None
        self.busy_foreign_pol = float(spec["busy_foreign_gib"]) if spec.get("busy_foreign_gib") is not None else None
        self.guard_policy = bool(spec.get("gpu_guard", True))   # Policy per node: Router respects the GPU guard status (cap, score, HA)
        self.foreign_frozen = 0.0     # fremdes VRAM beim Start eines Ladevorgangs (siehe announce_load / foreign_vram_gib)
        self.ollama_version = None    # Stage 6: Supply Chain (from /api/version, every 5 min)
        self.version_ts = 0.0
        # WOL
        self.last_wake = 0.0
        self.wake_lock = asyncio.Lock()

    def gpu_known(self, now):
        return self.hb_ts and (now - self.hb_ts) <= state.CFG.hb_stale_s

    def ollama_vram_gib(self):
        """What Ollama currently occupies: for each loaded model the measured actual occupancy at its loaded context
        (catalog `real`, since 0.3.5), else `/api/ps`. For the VRAM budget: count what actually frees up on eviction
        - with the too-small /api/ps number the router rejected a context switch of the same model
        that actually fit."""
        return self._loaded_real_gib()

    def _loaded_real_gib(self):
        total = 0.0
        for name, ps_gib in self.loaded.items():
            d = self.digest_of.get(name)
            real = state.CFG.real_gib(name, self.loaded_ctx.get(d) if d is not None else None)
            total += max(ps_gib, real) if real is not None else ps_gib
        return total

    def ollama_vram_claimed_gib(self):
        """What Ollama claims, including tail. `/api/ps` (poll) and total occupied from nvidia-smi
        (heartbeat) lag each other, in both directions: on unload /api/ps drops to zero immediately, while
        nvidia-smi still shows memory; on load nvidia-smi sees the model before it appears in /api/ps.
        Both would appear as foreign VRAM. While the tail runs, the higher value applies; once nvidia-smi
        has caught up, `foreign_vram_gib()` clamps it to 0 anyway."""
        now = time.time()
        claim = self.load_claim_gib(now)
        if now < self.vram_hold_until:
            claim = max(claim, self.vram_hold_gib)
        return max(self._loaded_real_gib(), claim)

    def hold_vram(self, now, gib):
        """Set tail: for `vram_settle_s` at least `gib` counts as Ollama occupancy."""
        if gib > 0:
            self.vram_hold_gib = max(gib, self.vram_hold_gib if now < self.vram_hold_until else 0.0)
            self.vram_hold_until = now + state.CFG.vram_settle_s

    def announce_load(self, model, ctx):
        """Router is triggering a load: expected occupancy counts as claimed immediately.
        Without this, the loading model counts as foreign VRAM - and a cold start taking longer than
        `foreign_sustain_s` would make the node busy mid-load; the safety net then unloads exactly
        the model just loaded. The claim holds UNTIL the model is in /api/ps (not just `vram_settle_s`,
        since that slow cold start is the dangerous case); `finish_load` ends it with a short tail,
        the deadline catches a lost completion.

        Expected occupancy after load, i.e. `need_gib` WITHOUT the fit margin (since 0.3.5 for measured
        models the actual nvidia-smi occupancy, else what `/api/ps` reports) - and
        NOT current occupancy plus new: Ollama evicts on load, the sum would be nearly double
        reality and would disable busy detection for the duration of the claim. If Ollama
        holds another model, `max(...)` with the `/api/ps` sum covers it."""
        if not self.loading:
            self.foreign_frozen = self.foreign_vram_gib()   # Freeze state BEFORE the claim
        real = state.CFG.real_gib(model, ctx)   # measured: without margin, else need_gib minus fit margin
        claim = real if real is not None else max(0.0, state.CFG.need_gib(model, ctx, self) - state.CFG.overhead_gib)
        self.loading[model] = (claim, time.time() + 300)

    def finish_load(self, model):
        """Load finished (also on error): convert claim to a short tail, covering the gap
        until the next /api/ps poll."""
        claim = self.loading.pop(model, None)
        if claim:
            self.hold_vram(time.time(), claim[0])

    def load_claim_gib(self, now):
        return max((gib for gib, deadline in self.loading.values() if now < deadline), default=0.0)

    def note_loaded_changed(self, now):
        """Call after each poll: if Ollama occupancy shrinks, start the tail (with actual occupancy,
        else the tail would end at the /api/ps number and the rest would briefly appear as foreign VRAM)."""
        cur = self._loaded_real_gib()
        if cur < self.vram_last_gib - 0.05:
            self.hold_vram(now, self.vram_last_gib)
        self.vram_last_gib = cur
        for m in [m for m in self.loading if self.is_loaded(m)]:
            self.finish_load(m)   # /api/ps confirms it: convert claim to a short tail

    def is_loaded(self, model):
        """Loaded = same digest, regardless of name (an alias and its base model)."""
        d = self.digest_of.get(model)
        return (d is not None and d in self.loaded_digest) or model in self.loaded

    def loaded_size(self, model):
        d = self.digest_of.get(model)
        if d is not None and d in self.loaded_digest:
            return self.loaded_digest[d]
        return self.loaded.get(model, 0.0)

    def loaded_context(self, model):
        d = self.digest_of.get(model)
        return self.loaded_ctx.get(d) if d is not None else None

    def same_blob(self, a, b):
        da, db = self.digest_of.get(a), self.digest_of.get(b)
        return a == b or (da is not None and da == db)

    def foreign_vram_gib(self):
        """VRAM not occupied by Ollama, the agent's own children, or known desktop baseline."""
        if self.vram_used_gib is None:
            return 0.0
        if self.ollama_proc_gib is not None:
            ollama = self.ollama_proc_gib
        else:
            ollama = self.ollama_vram_claimed_gib()
        # The agent's children (2026-09-30: Whisper via CUDA, 2.6 GiB) report as fact; without this they would count as game and
        # push the node over the foreign threshold - with desktop + one game 3.6 GiB foreign, with Whisper 6.2 GiB.
        raw = max(0.0, self.vram_used_gib - ollama - self.children_vram_gib - self.baseline_gib())
        # During a router-triggered load, the claim (announce_load) covers the whole model, even
        # while it is not yet in VRAM - a game thus disappeared from the calculation for 300 s (gpu-laptop 2026-09-11:
        # foreign 0.0 despite 2.4 GiB game, node stayed free). Hence at least the pre-load value applies.
        if self.load_claim_gib(time.time()) > 0:
            return max(raw, self.foreign_frozen)
        return raw

    def baseline_gib(self):
        """Desktop baseline without Ollama: policy value (Config/UI) and learned value (rolling 24-h minimum of
        occupied minus Ollama); if both exist, the minimum applies - a game on first start cannot spoil the learning."""
        b = self.foreign_baseline_gib
        if self.reg is not None:
            pol = self.reg.get("policy") or {}
            if pol.get("foreign_vram_baseline_gib") is not None:
                b = float(pol["foreign_vram_baseline_gib"])
            learned = (self.reg.get("learned") or {}).get("baseline_gib")
            if learned is not None:
                b = min(b, learned) if b else learned
        return b

    def learn_baseline(self, now, sample):
        """Learn desktop baseline: per hour count/sum/min of (VRAM used - Ollama) in
        `nodes.json`, then the **median of hourly means** over 24 h.

        Until 2026-09-08 it was the global minimum. That drifts to the quietest moment of the day - on
        gpu-desktop 2.89 GiB with a sleeping monitor, while the active desktop needs 6.1 GiB. The difference was
        treated as foreign VRAM and made the node busy for no reason (twice in four minutes, GPU utilization 0). A
        minimum does not describe normal operation, but its extreme.

        Two layers against contamination: data is only collected when the GPU is idle (filter at the caller), and the
        median over hours catches individual corrupted hours. Outliers on the high side (a game)
        can only shift the median if they constitute the majority of hours - and then the policy value in `baseline_gib()`
        caps it anyway."""
        if self.reg is None or sample is None:
            return
        learned = self.reg.setdefault("learned", {})
        buckets = learned.setdefault("buckets", {})
        hour = int(now // 3600)
        # Discard buckets in the old format (single number = hourly minimum) instead of converting: they were created WITHOUT
        # the idle filter, so they contain gaming hours. On gpu-desktop that would be 19 of 23 hours at
        # 18.2 GiB - the median from that would be worse than everything we are replacing.
        for k, _v in [(k, v) for k, v in buckets.items() if not isinstance(v, dict)]:
            del buckets[k]
        b = buckets.get(str(hour)) or {"n": 0, "sum": 0.0, "min": 1e9}
        buckets[str(hour)] = {"n": b["n"] + 1, "sum": round(b["sum"] + sample, 3), "min": round(min(b["min"], sample), 3)}
        for k in [k for k in buckets if int(k) < hour - 24]:
            del buckets[k]
        means = sorted(v["sum"] / v["n"] for v in buckets.values() if v["n"])
        mid = len(means) // 2
        learned["baseline_gib"] = round(means[mid] if len(means) % 2 else (means[mid - 1] + means[mid]) / 2, 2)
        learned["samples"] = learned.get("samples", 0) + 1
        state.REG.dirty = True

    def apply_spec(self, spec):
        """Adopt facts/policy of a registered node (registration or policy change in the UI)."""
        if spec.get("vram_total_gib"):
            self.vram_total_gib = float(spec["vram_total_gib"])
        if spec.get("mac"):
            self.mac = spec["mac"]
        self.wol = bool(spec.get("wol", self.wol))
        self.weight = int(spec.get("weight", self.weight))
        if spec.get("gpu"):
            self.gpu = spec["gpu"]
        self.openai_upstream = spec.get("upstream_api") == "openai"   # agent may switch between Ollama and an OpenAI server
        self.upstream_releasable = spec.get("upstream_releasable") is True
        self.upstream_fallback = spec.get("upstream_fallback") is True
        if spec.get("foreign_vram_baseline_gib") is not None:
            self.foreign_baseline_gib = float(spec["foreign_vram_baseline_gib"])
        self.max_inflight = int(spec["max_inflight"]) if spec.get("max_inflight") else None
        self.busy_util_pct = float(spec["busy_gpu_util_pct"]) if spec.get("busy_gpu_util_pct") is not None else None
        self.busy_foreign_pol = float(spec["busy_foreign_gib"]) if spec.get("busy_foreign_gib") is not None else None
        self.guard_policy = bool(spec.get("gpu_guard", True))   # Policy per node: Router respects the GPU guard status (cap, score, HA)

    def busy_util_threshold(self):
        return self.busy_util_pct if self.busy_util_pct is not None else state.CFG.busy_util

    def busy_exit_util_threshold(self):
        """Hysteresis: below this utilization a busy node may become free again - never higher than its admission threshold."""
        return min(state.CFG.busy_exit_util, self.busy_util_threshold())

    def busy_foreign_threshold(self):
        return self.busy_foreign_pol if self.busy_foreign_pol is not None else state.CFG.busy_foreign_gib

    # --- Stage 3: Circuit Breaker / Admission ---
    def effective_max_inflight(self):
        base = self.max_inflight or state.CFG.admission["max_inflight_default"]
        g = state.CFG.gpu_guard
        if not g["enabled"] or not self.guard_policy:
            return base
        gs = self.guard_state()
        if gs == "throttled" or (gs is None and g["require_fresh_status"]):
            return min(base, g["throttled_max_inflight"])
        return base

    def guard_state(self, now=None):
        """GPU guard status according to the agent; None = no or stale status (older than 30 s)."""
        if not self.guard or not self.guard_ts or (now or time.time()) - self.guard_ts > 30:
            return None
        return self.guard.get("state")

    def guard_view(self, now=None):
        """For /admin/state, HA and UI: status plus policy and freshness."""
        g = self.guard or {}
        return {"state": self.guard_state(now), "reported": g.get("state"), "limit_w": g.get("limit_w"), "target_w": g.get("target_w"),
                "default_limit_w": g.get("default_limit_w"), "problem": bool(g.get("problem")), "reason": g.get("reason"),
                "warnings": g.get("warnings") or [], "source": g.get("source"), "high_load_s": g.get("high_load_s"),
                "policy": self.guard_policy, "router_enabled": state.CFG.gpu_guard["enabled"]}

    def ollama_push_active(self, now):
        """Does the agent report the Ollama status itself and is the last report fresh? Then the router does not poll."""
        return bool(self.ollama_push_ts) and now - self.ollama_push_ts < PUSH_STALE_S

    def ps_fresh(self, now):
        """Is it known which models are loaded? Push: changes arrive immediately, fresh as long as the agent reports.
        Poll: only until shortly after the next tick."""
        return self.ollama_push_active(now) or now - self.polled_ok < 2 * state.CFG.poll_s + 1

    def go_offline(self, why):
        """Remove node from routing: reset state, loaded models, load requests and VRAM tail.
        One place for poll errors, tunnel drops and locks (previously four copies with slightly different scope)."""
        if self.state != "offline":
            log.warning("node %s -> offline (%s)", self.name, why)
        self.state, self.hot_since, self.calm_since = "offline", None, None
        self.prewarm_after_game = False   # online plans its own prewarming
        self.loaded, self.loaded_digest, self.loaded_ctx, self.loading, self.ram_spill = {}, {}, {}, {}, {}
        self.vram_last_gib, self.vram_hold_gib, self.vram_hold_until = 0.0, 0.0, 0.0   # offline: tail is irrelevant
        state.MQTT_DIRTY.append(True)

    def note_perf(self, model, s, now):
        """Rates of a warm request for the HA sensors (slowness watch). Each rate needs its own minimum size: a reply of a
        few tokens or a prompt mostly served from the cache gives a noisy rate. Only a changed value counts as news."""
        changed = False
        if s.get("tokens", 0) >= PERF_MIN_GEN_TOKENS:
            self.last_gen, changed = {"model": model, "tps": s["gen_tps"], "ts": now}, True
        if s.get("prompt_tps") is not None and (s.get("prompt_tokens") or 0) >= PERF_MIN_PROMPT_TOKENS:
            self.last_prompt, changed = {"model": model, "tps": s["prompt_tps"], "ts": now}, True
        if changed:
            state.MQTT_DIRTY.append(True)

    def budget_gib(self, now, model):
        """What Ollama may occupy on this node: free VRAM + everything Ollama itself holds (will be
        evicted or reused via Runner-Sharing as needed) - reserve. Foreign VRAM (desktop, game) does not count."""
        reserve = state.CFG.reserve.get(self.state, 1.0)
        if self.gpu_known(now) and self.vram_free_gib is not None:
            return self.vram_free_gib + self.ollama_vram_gib() - reserve
        return self.vram_total_gib - reserve

    @property
    def speaks_openai(self):
        """The tunnel reaches an OpenAI-compatible server now - not while the standby Ollama stands in for it (then the
        node is an Ollama node until the router releases the server, openai_node.py)."""
        return self.openai_upstream and self.upstream_serving != openai_node_serving_ollama

    def serves_openai(self, model):
        """Requests for this model go out in OpenAI form: the node's upstream is an OpenAI-compatible server and the model
        is that server's. While the standby Ollama stands in, the server's models stay listed (digest "openai:<id>", so a
        request can release it) and every other model is the standby's."""
        if not self.speaks_openai:
            return self.openai_upstream and (self.digest_of.get(model) or "").startswith("openai:")
        return True

    def snapshot(self, now):
        return {
            "state": self.state, "busy_reason": self.busy_reason,
            "power": self.power, "power_since": self.power_since,
            "models": sorted(self.models), "loaded": {k: round(v, 2) for k, v in self.loaded.items()},
            "ram_spill_gib": {k: round(v, 2) for k, v in self.ram_spill.items()},   # loaded models partly in system RAM
            "upstream": ({"api": "openai", "releasable": self.upstream_releasable, "state": self.upstream_state or "answering",
                          "last_order": self.upstream_order, "fallback": self.upstream_fallback,
                          "serving": self.upstream_serving or None} if self.openai_upstream else None),
            "last_gen": self.last_gen, "last_prompt": self.last_prompt,
            "inflight": self.inflight, "gpu_known": bool(self.gpu_known(now)),
            "gpu_util": self.gpu_util, "vram_free_gib": self.vram_free_gib,
            "vram_used_gib": self.vram_used_gib, "vram_total_gib": self.vram_total_gib, "foreign_vram_gib": round(self.foreign_vram_gib(), 2), "children_vram_gib": round(self.children_vram_gib, 2),
            "heartbeat_age_s": round(now - self.hb_ts, 1) if self.hb_ts else None,
            "wol": self.wol, "tunnel": self.tunnel is not None, "tls": self.tls, "fingerprint": self.fp[:16] if self.fp else None, "baseline_gib": round(self.baseline_gib(), 2), "weight": self.weight, "tls_fingerprint": self.tls_fp[:16] if self.tls_fp else None, "last_wake_age_s": round(now - self.last_wake, 1) if self.last_wake else None,
            "gpu": self.gpu, "sensors": self.sensors, "gpu_guard": self.guard_view(now), "loaded_names_by_digest": sorted(m for m in self.models if self.is_loaded(m)),
            "breaker": self.breaker, "max_inflight": self.effective_max_inflight(), "draining": now < self.draining_until, "ollama_source": "push" if self.ollama_push_active(now) else "poll",
            "game": self.game, "memory": self.memory, "memory_pressure": self.mem_pressure,   # Agent >= 0.17.0
            "game_heavy": self.game_heavy, "game_heavy_why": self.game_heavy_why,             # 0.8.7
            "routes": self.routes,
        }
