"""config.yaml + roles.yaml (UI-Overrides) einlesen, validieren, live neu laden."""

import difflib
import hashlib
import os
import re
import ssl
import time
import yaml

from . import state
from .session_config import SessionConfig, parse_session_config
from .shared_config import parse_scopes
from .context_config import ContextConfig, parse_context_config
from .memory_config import CLIENT_MEMORY_FIELDS, PROJECT_FIELDS, HindsightConfig, parse_hindsight, parse_client_memory
from .rerank_config import RerankConfig, parse_rerank_config
from .privacy import FIELDS as PRIVACY_FIELDS, parse_privacy_config
from . import settings as settings_mod
from .common import DATA_CLASSES_DEFAULT, GIB, PRIORITIES, THINK_MODES, log, tls_fingerprint
from .outcome import Problem, failed, invalid


def _d(path):
    """Runtime default of a key from settings.EDITABLE - a single source for parsers, UI, and base value display."""
    return settings_mod.EDITABLE[path]["default"]


# Performance (2026-09-11): yaml.safe_load is the pure Python parser (config.yaml 15.6 ms on Router-CT), the libyaml loader
# takes 1.5 ms. Plus an mtime-based cache: /admin/config read config.yaml and roles.yaml twice per call (~86 ms),
# and the UI polls every few seconds. Write paths (write_overrides) invalidate via changed mtime.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_YAML_CACHE = {}   # pfad -> (mtime_ns, size, geparster Baum)


def load_yaml(path, default=None):
    """Return parsed YAML file - from cache as long as mtime/size unchanged. Returns a
    deep copy? No: callers must NOT modify the tree (merged() works on its own copy)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:  # exception-ok: file system; exists() then stat() would race with a writer replacing the file
        return default
    key = (st.st_mtime_ns, st.st_size)
    hit = _YAML_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f, Loader=_YAML_LOADER)
    if data is None:
        data = default
    _YAML_CACHE[path] = (key, data)
    return data

# Stage 6: Config schema. dict = allowed subkeys, None = leaf (any value, type check in parse),
# "*" = any names. Unknown keys are an error (typos like `limitz` were previously silently ignored).
_TIER = {"model": None, "num_ctx": None, "busy_ok": None}
_CANARY = {"model": None, "num_ctx": None, "percent": None, "busy_ok": None}
# from fields accepted by parse_client_memory - single source, otherwise schema rejects valid keys
_MEMORY_SCHEMA = {**dict.fromkeys(CLIENT_MEMORY_FIELDS), "projects": {"*": dict.fromkeys(PROJECT_FIELDS)}}
SCHEMA = {
    "router": {
        "listen": None, "control_listen": None, "public_url": None, "control_tls": {"cert": None, "key": None},
        "ollama_poll_s": None, "heartbeat_interval_s": None, "heartbeat_stale_s": None, "offline_after_misses": None,
        "agent_missing_problem_s": None, "ram_spill_problem_gib": None, "expose_concrete_models": None, "request_timeout_s": None,
        "toolcall_rescue": None,
        "memory_scopes": {"*": {"data_class": None, "allow_cloud": None}},
        "hindsight": {k: None for k in HindsightConfig.__dataclass_fields__},
        "context_manager": {k: None for k in ContextConfig.__dataclass_fields__},
        "session_store": {k: None for k in SessionConfig.__dataclass_fields__},
        "rerank": {k: None for k in RerankConfig.__dataclass_fields__},
        "limits": {"max_images": None, "max_tools": None, "max_messages": None}, "session_affinity_ttl_s": None,
        "idempotency_ttl_s": None, "control_auth": {"users": None}, "heartbeat_token": None, "api_tls": None,
        "client_auth": {"mode": None, "audit_log": None, "locked": None,
                        "clients": {"*": {"token_sha256": None, "ip": None, "roles": None, "models": None, "requests_per_minute": None, "max_priority": None,
                                          "cloud": None, "data_class": None, "privacy_check": None, "rerank": None, "inference": None, "session_projects": None, "session_handoff_from": None, "memory": _MEMORY_SCHEMA, "note": None, "token_rotated": None, "created": None}}},
        "cloud": {"enabled": None, "data_classes": None, "default_data_class": None, "max_cloud_data_class": None, "credential_scan": None,
                  "egress_allow": None, "privacy_check": dict.fromkeys(PRIVACY_FIELDS),
                  "providers": {"*": {"kind": None, "base_url": None, "api_key_env": None, "secrets_file": None, "enabled": None, "budget_month_chf": None,
                                      "warn_at_percent": None, "region": None, "timeout_s": None, "max_inflight": None, "max_data_class": None}}},
        "openai": {"enabled": None, "default_think": None},
        "anthropic": {"enabled": None, "keepalive_s": None, "think": None},
        "wol": {"wait_up_s": None, "retry_interval_s": None, "cooldown_s": None, "broadcast_addresses": None},
    },
    "mqtt": {"host": None, "port": None, "tls": None, "username": None, "password": None, "password_env": None, "password_file": None,
             "base_topic": None, "discovery_prefix": None, "interval_s": None},
    "modes": {
        "busy_enter": {"gpu_util_pct": None, "sustain_s": None, "util_requires_foreign_gib": None, "or_foreign_vram_gib": None, "foreign_sustain_s": None,
                       "game": None, "game_sustain_s": None, "game_min_gpu_gib": None, "game_min_gpu_pct": None},
        "busy_exit": {"below_for_s": None, "gpu_util_pct": None, "game_quiet_s": None},
        "memory_pressure": {"min_free_gib": None, "polls": None}, "vram_reserve_gib": {"free": None, "busy": None}, "keep_alive": {"free": None, "busy": None},
        "unload_on_busy": None, "unload_on_busy_interval_s": None, "vram_settle_s": None, "fit_overhead_gib": None, "warm_first": None,
        "prewarm": {"on_free": None, "free_delay_s": None, "on_online": None, "online_delay_s": None, "residency_idle_s": None, "residency_check_s": None},
        "score": None, "breaker": {"failures": None, "window_s": None, "open_s": None},
        "admission": {"max_inflight_default": None, "aging_s": None, "max_wait_s": None, "max_queue": None,
                      "max_wait_interactive_s": None, "max_wait_batch_s": None},
        "gpu_guard": {"enabled": None, "throttled_max_inflight": None, "score_penalty": None, "require_fresh_status": None},
        "agent_update": {"enabled": None, "canary": None, "canary_clean_h": None, "public_key": None},
        "ollama_update": {"enabled": None, "canary": None, "canary_clean_h": None, "window_start": None, "window_end": None,
                          "timezone": None, "check_interval_h": None, "release_url": None},
        "upstream_window": {"enabled": None, "window_start": None, "window_end": None, "timezone": None, "idle_hold_s": None},
    },
    "models": {"*": {"weights_gib": None, "kv_gib_per_1k": None, "capabilities": None, "source": None, "note": None, "measured_at": None,
                     "measured_on": None, "vram_gib_8k": None, "vram_gib_32k": None, "partial_offload": None,
                     # since 0.3.5: real = weights/kv from nvidia-smi delta (not /api/ps), both values per context
                     "real": None, "vram_real_gib": None, "vram_ps_gib": None,
                     "cloud": None, "provider_model": None, "price_chf_per_m": None, "context_tokens": None, "reasoning": None,
                     # since 0.6.5: max concurrent requests per node (qwen3.8: 1, Ollama cannot parallelize qwen35)
                     "max_parallel": None}},
    "nodes": {"*": {"ollama": None, "vram_total_gib": None, "wol": None, "weight": None, "mac": None, "foreign_vram_baseline_gib": None,
                    "gpu": None, "ollama_tls_fingerprint": None, "ollama_tls_cert": None, "max_inflight": None}},
    "roles": {"*": {"exposed_as": None, "tiers": [_TIER], "latency_first": None, "priority": None, "canary": _CANARY, "shadow": _CANARY}},
    # Decision Engine (2026-09-16): Auto-role, whose role selects a chain of engines (decision/__init__.py)
    "decision_engine": {"enabled": None, "role": None, "options": None, "default": None, "chain": None, "context_chars": None,
                        "policy": {"min_top_probability": None, "min_margin": None, "max_entropy_ratio": None},
                        "jevlike": {"endpoint": None, "model": None, "timeout_s": None},
                        "local_llm": {"model": None, "timeout_s": None, "descriptions": None},
                        "tfidf": {"model_path": None},
                        "embed": {"endpoint": None, "model": None, "timeout_s": None},
                        "rules": None, "calibration_path": None,
                        "capture": {"enabled": None, "path": None, "clients": None, "anonymize": None, "max_context_chars": None},
                        # 0.6.7: Observation mode - wait for request, classify and log if a deeper free
                        # tier would have sufficed (fallback_observation.py); observe_light = classes where this applies
                        "observe_blocked": None, "observe_light": None,
                        # ... and observe_all: classify EVERY request in background, one line per request in observe_path
                        # (default anfragen.jsonl next to config.yaml) - class, duration, tokens, no content
                        "observe_all": None, "observe_path": None},
}
DECISION_ENGINES = ("rules", "jevlike", "local_llm", "tfidf", "embed")


def validate_schema(c, schema=SCHEMA, path=""):
    """Name unknown keys (with suggestion), sections must be mappings. Returns error list."""
    errs = []
    if not isinstance(c, dict):
        return [f"{path or '<root>'}: must be a mapping"]
    for k, v in c.items():
        sub = schema.get(k, schema.get("*", "MISSING"))
        here = f"{path}.{k}" if path else str(k)
        if sub == "MISSING":
            hint = difflib.get_close_matches(str(k), [s for s in schema if s != "*"], n=1, cutoff=0.6)
            errs.append(f"unknown key {here}" + (f" - did you mean '{hint[0]}'?" if hint else "") +
                        f" (allowed: {', '.join(sorted(s for s in schema if s != '*'))})")
            continue
        if isinstance(sub, dict) and v is not None:
            errs += validate_schema(v, sub, here)
        elif isinstance(sub, list) and v is not None:
            if not isinstance(v, list):
                errs.append(f"{here}: must be a list")
            else:
                for i, item in enumerate(v):
                    errs += validate_schema(item, sub[0], f"{here}[{i}]")
    return errs


# Sizing fields of a catalog entry belong together: one measurement (UI, perf) provides them as a unit, and mixing new weights with
# an old measurement (real, vram_real_gib) would imply the old measurement for new values.
SIZING_FIELDS = ("weights_gib", "kv_gib_per_1k", "real", "vram_real_gib", "vram_ps_gib", "vram_gib_8k", "vram_gib_32k",
                 "measured_at", "measured_on", "partial_offload")


def merge_models(base, overlay):
    """Catalog from config.yaml + roles.yaml, field-by-field per model (0.6.9). Until 0.6.8 a roles.yaml entry replaced the config.yaml one
    entirely - max_parallel from config.yaml did not apply to qwen3.8 because roles.yaml held a model measurement
    (2026-10-02). If the overlay entry has sizing fields, they replace the base ones as a unit (SIZING_FIELDS);
    all other fields (max_parallel, capabilities, note, ...) remain from base if overlay does not set them."""
    out = dict(base)
    for m, e in overlay.items():
        b = base.get(m)
        if not isinstance(b, dict) or not isinstance(e, dict):
            out[m] = e
            continue
        if any(f in e for f in SIZING_FIELDS):
            b = {k: v for k, v in b.items() if k not in SIZING_FIELDS}
        out[m] = {**b, **e}
    return out


class Config:
    """config.yaml (commented, via deploy) + roles.yaml (written by UI, overrides roles/models/expose)."""

    def __init__(self, path, use_overrides=True):
        """For callers without Problem path (tests, tools). Config.load() is the explicit form and returns an
        invalid configuration as Problem."""
        self._locate(path, use_overrides)
        problem = self.reload()
        if failed(problem):
            raise ValueError(problem.message)  # exception-ok: a constructor has no return channel; Config.load returns it

    @classmethod
    def load(cls, path, use_overrides=True):
        """Config or Problem 400 (invalid config) - start, --check and reload decide on this."""
        cfg = cls.__new__(cls)
        cfg._locate(path, use_overrides)
        problem = cfg.reload()
        return problem if failed(problem) else cfg

    def _locate(self, path, use_overrides):
        self.path = path
        self.overrides_path = os.path.join(os.path.dirname(os.path.abspath(path)), "roles.yaml")
        self.use_overrides = use_overrides

    def merged(self, overrides=None):
        import copy
        c = copy.deepcopy(load_yaml(self.path))   # eigene Kopie: merged() schreibt hinein
        if overrides is None and self.use_overrides and os.path.exists(self.overrides_path):
            overrides = copy.deepcopy(load_yaml(self.overrides_path, {}) or {})
        for k, v in (overrides or {}).items():
            if k == "roles":
                # UI writes only tiers/latency_first. Everything else on a role (priority, canary, shadow) remains from
                # config.yaml - otherwise `assist` would lose priority once roles.yaml existed (2026-09-10).
                base = c.get("roles") or {}
                c["roles"] = {name: {**(base.get(name) or {}), **(r or {})} for name, r in (v or {}).items()}
            elif k == "models":
                c["models"] = merge_models(c.get("models") or {}, v or {})
            elif k == "expose_concrete_models":
                c["router"]["expose_concrete_models"] = bool(v)
        # Clients from UI (new clients, rotated secrets): per client via config.yaml entry, fields individually
        if (overrides or {}).get("clients"):
            ca = c.setdefault("router", {}).setdefault("client_auth", None) or {}
            c["router"]["client_auth"] = ca
            cl = ca.get("clients") or {}
            ca["clients"] = cl
            for name, spec in (overrides["clients"] or {}).items():
                cl[str(name)] = {**(cl.get(name) or {}), **(spec or {})}
        # Stage 7 (UI settings): settings {path: value} last - roles from roles.yaml are known then (client roles)
        if (overrides or {}).get("settings"):
            applied = settings_mod.apply(c, overrides["settings"])
            if failed(applied):
                return applied
        return c

    def base(self):
        """config.yaml pure (without roles.yaml) - for base value display in UI. Read-only, do not modify (cache)."""
        return load_yaml(self.path)

    def reload(self, overrides=None):
        """Reload; None = adopted, Problem = invalid and NOTHING changed. First a probe, then this object:
        until 0.8.9 parse() aborted mid-override, and "config reload failed, keeping old" kept a half-new
        configuration."""
        import copy
        c = self.merged(overrides)
        if failed(c):
            return c
        probe = Config.__new__(Config)
        probe._locate(self.path, self.use_overrides)
        problem = probe.parse(copy.deepcopy(c))
        if failed(problem):
            return problem
        return self.parse(c)

    def parse(self, c):
        """Check and adopt tree c. None = ok, Problem = invalid (then object may be partially populated -
        hence reload() checks on a probe first)."""
        errs = validate_schema(c)
        if errs:
            return invalid("configuration: " + "; ".join(errs))
        for sect in ("router", "modes", "roles"):
            if not isinstance(c.get(sect), dict):
                return invalid(f"configuration: section {sect} missing")
        self.raw = c
        r = c["router"]
        for step in (lambda: self._parse_router(r), lambda: self._parse_client_auth(r.get("client_auth") or {})):
            if failed(problem := step()):
                return problem
        for attr, value in (("session_store", parse_session_config(r.get("session_store"))),
                            ("context_manager", parse_context_config(r.get("context_manager")))):
            if failed(value):
                return value
            setattr(self, attr, value)
        if self.context_manager.enabled and not self.session_store.enabled:
            return invalid("context_manager requires session_store.enabled")
        if failed(problem := self._parse_cloud(r.get("cloud") or {})):
            return problem
        for attr, value in (("hindsight", parse_hindsight(r.get("hindsight"))),
                            ("rerank", parse_rerank_config(r.get("rerank"))),   # POST /rerank (rerank.py), default off
                            ("memory_scopes", parse_scopes(r.get("memory_scopes"), self.cloud["data_classes"]))):
            if failed(value):
                return value
            setattr(self, attr, value)
        if self.hindsight.outbox_enabled and not (self.hindsight.enabled and self.session_store.enabled):
            return invalid("persistent memory outbox requires enabled hindsight and session_store")
        for client in self.client_auth["clients"].values():
            sources = client.get("session_handoff_from", [])
            if not isinstance(sources, list) or any(not isinstance(v, str) or v not in self.client_auth["clients"] for v in sources):
                return invalid("session_handoff_from must list configured principals")
            granted = parse_client_memory(client.get("memory"), self.cloud["data_classes"])
            if failed(granted):
                return granted
            client["memory"] = granted
            for grant in client["memory"]["projects"].values():
                if grant["scope"]:
                    shared = self.memory_scopes.get(grant["scope"])
                    if not shared or shared["data_class"] != grant["data_class"]:
                        return invalid("shared scope classification must match the project grant")
                    if grant["allow_private_addresses"] and shared["allow_cloud"]:
                        # another client of the shared bank could take its addresses into cloud inference
                        return invalid("allow_private_addresses requires a shared scope with allow_cloud false")
        self.mqtt = c.get("mqtt") or None   # Home Assistant per MQTT-Discovery (optional): host, port, username, password_env, base_topic, interval_s
        self._parse_wol(r.get("wol") or {})
        for step in (lambda: self._parse_modes(c["modes"]), lambda: self._parse_models(c.get("models") or {})):
            if failed(problem := step()):
                return problem
        bad_fp = [n for n, spec in c["nodes"].items() if spec.get("ollama_tls_fingerprint") and not tls_fingerprint(spec["ollama_tls_fingerprint"])]
        if bad_fp:
            return invalid(f"nodes.{bad_fp[0]}.ollama_tls_fingerprint: SHA-256 expected (64 hex digits, colons allowed)")
        self.nodes = c["nodes"]
        if failed(problem := self._parse_roles(c["roles"])):
            return problem
        self.busy_ok_models = {t["model"] for r in self.roles.values() for t in r["tiers"] if t["busy_ok"]}
        decision = self._parse_decision(c.get("decision_engine") or {})
        if failed(decision):
            return decision
        self.decision = decision
        return None

    def _parse_router(self, r):
        self.listen = r.get("listen", "0.0.0.0:11434")
        self.control_listen = r.get("control_listen", "0.0.0.0:11435")
        self.poll_s = float(r.get("ollama_poll_s", _d("router.ollama_poll_s")))
        self.offline_after = int(r.get("offline_after_misses", _d("router.offline_after_misses")))
        self.hb_interval_s = float(r.get("heartbeat_interval_s", _d("router.heartbeat_interval_s")))   # Agent tick (provisioned)
        self.hb_stale_s = float(r.get("heartbeat_stale_s", _d("router.heartbeat_stale_s")))
        if self.hb_stale_s < 2 * self.hb_interval_s:   # otherwise a node between two reports is considered "GPU unknown"
            self.hb_stale_s = 2 * self.hb_interval_s + 1
        self.agent_missing_s = float(r.get("agent_missing_problem_s", _d("router.agent_missing_problem_s")))   # Agent was there and silent -> HA problem
        self.ram_spill_problem_gib = float(r.get("ram_spill_problem_gib", _d("router.ram_spill_problem_gib")))   # model in system RAM -> HA problem
        self.expose_concrete = bool(r.get("expose_concrete_models", False))
        self.request_timeout_s = float(r.get("request_timeout_s", _d("router.request_timeout_s")))
        self.heartbeat_token = r.get("heartbeat_token") or ""
        self.api_tls = bool(r.get("api_tls", False))   # 11434 also TLS (cert from control_tls)
        # Rescue tool calls stranded in text (see toolcall_rescue). Default on:
        # tier applies only if Ollama parsed nothing itself, otherwise leaves everything untouched.
        self.toolcall_rescue = bool(r.get("toolcall_rescue", _d("router.toolcall_rescue")))
        # OpenAI-compatible interface /v1 (Node-RED-MCP, other OpenAI clients). Router translates to
        # /api/chat itself, because Ollama's own /v1 does not know num_ctx/keep_alive (model would reload with default context).
        oa = r.get("openai") or {}
        self.openai_enabled = bool(oa.get("enabled", True))
        # think, if client says nothing: false = response without invisible thinking (like HA AI-Tasks);
        # true = model default; null = do not set field (Ollama decides)
        self.openai_default_think = oa.get("default_think", _d("router.openai.default_think"))
        # Anthropic Messages API /v1/messages (Claude Code). Ping interval while the node is still computing the prompt:
        # Ollama sends headers only with the first token; with 196k context, this can take up to ~2 min.
        an = r.get("anthropic") or {}
        self.anthropic_enabled = bool(an.get("enabled", True))
        self.anthropic_keepalive_s = float(an.get("keepalive_s", _d("router.anthropic.keepalive_s")))
        # Thinking: auto = as requested by the client (thinking block), on/off = fixed. Claude Code requires adaptive thinking for every turn;
        # qwen3.8 only knows on/off and always thinks when enabled.
        self.anthropic_think = str(an.get("think", _d("router.anthropic.think")))
        if self.anthropic_think not in THINK_MODES:
            return invalid(f"router.anthropic.think must be one of {THINK_MODES}, not {self.anthropic_think!r}")
        # Basic Auth for UI + /admin/*: {user: "pbkdf2:<iter>:<salt_hex>:<hash_hex>"}; empty = open (tests only)
        self.control_users = dict((r.get("control_auth") or {}).get("users") or {})
        # TLS for the Control port: {cert: <fullchain.pem>, key: <key.pem>}; missing block -> plaintext (tests only)
        tls = r.get("control_tls") or {}
        self.control_tls = (tls.get("cert"), tls.get("key")) if tls.get("cert") and tls.get("key") else None
        self.public_url = r.get("public_url", "")
        # Stage 1: Request size limits (413 before backend) and session affinity expiry
        lim = r.get("limits") or {}
        self.limits = {k: int(lim.get(k, _d(f"router.limits.{k}"))) for k in ("max_images", "max_tools", "max_messages")}
        self.session_ttl_s = float(r.get("session_affinity_ttl_s", _d("router.session_affinity_ttl_s")))
        self.idempotency_ttl_s = float(r.get("idempotency_ttl_s", _d("router.idempotency_ttl_s")))   # Stage 6: Retries with Idempotency-Key

    def _parse_client_auth(self, ca):
        # Client authentication on API port 11434 (Stage 2, design/roadmap.md). mode: observe = serve all,
        # count unknowns and log to audit; enforce = 401 without valid identity. clients: {name: {token_sha256,
        # ip: [..], roles: [..]|["*"], models: bool, requests_per_minute}}. No clients block = port open as before.
        mode = ca.get("mode", _d("router.client_auth.mode"))
        if mode not in ("observe", "enforce"):
            return invalid(f"client_auth.mode must be observe or enforce, not {mode!r}")
        clients = {}
        for name, spec in (ca.get("clients") or {}).items():
            spec = dict(spec or {})
            th = (spec.get("token_sha256") or "").strip().lower()
            if th and (len(th) != 64 or any(ch not in "0123456789abcdef" for ch in th)):
                return invalid(f"client_auth.clients.{name}.token_sha256 must be 64 hex characters")
            projects = spec.get("session_projects", [])
            if not isinstance(projects, list) or any(not isinstance(p, str) or not p.strip() or len(p) > 128
                                                     or any(ord(ch) < 32 for ch in p) for p in projects):
                return invalid("client_auth session_projects must be a list of nonempty project names (max 128 characters)")
            spec["session_projects"] = projects
            spec["token_sha256"] = th
            spec["ip"] = [str(x) for x in (spec.get("ip") or [])]
            spec["roles"] = [str(x) for x in (spec.get("roles") or ["*"])]
            spec["models"] = bool(spec.get("models", True))
            # Permission for POST /rerank (rerank.py), default no. Detection also via ip: Hindsight TEI client sends no header.
            if type(spec.get("rerank", False)) is not bool:
                return invalid(f"client_auth.clients.{name}.rerank must be true or false")
            spec["rerank"] = spec.get("rerank", False)
            # inference: false = only POST /rerank and GET /info (auth.inference_refusal); default true as before
            if type(spec.get("inference", True)) is not bool:
                return invalid(f"client_auth.clients.{name}.inference must be true or false")
            spec["inference"] = spec.get("inference", True)
            if spec["rerank"] and spec["inference"] and "*" in spec["roles"]:
                log.warning("client_auth.clients.%s: rerank: true without inference: false and without a roles limit - the client "
                            "may also call every role", name)
            if spec.get("requests_per_minute") is not None:
                spec["requests_per_minute"] = int(spec["requests_per_minute"])
            if spec.get("max_priority") is not None and spec["max_priority"] not in PRIORITIES:   # Stage 3: Class truncation
                return invalid(f"client_auth.clients.{name}.max_priority must be one of {PRIORITIES}")
            clients[str(name)] = spec
        # locked (Audit 2026-09-16): true = mode changeable ONLY via config.yaml + Deploy; UI setting and
        # POST /admin/client_auth are rejected (403/400 + audit entry). A compromised UI account cannot then
        # switch Client Auth to observe via a click.
        self.client_auth = {"mode": mode, "clients": clients, "locked": bool(ca.get("locked", False)),
                            "audit_log": ca.get("audit_log", "/var/log/skirnir-router/audit.jsonl")}

    def _parse_cloud(self, cl):
        # Stage 5: Cloud providers as tiers in roles (cloud.py). No per-client opt-in: the role allows it, the client can
        # opt out via routing.execution: local or clients.<c>.cloud: false.
        from urllib.parse import urlparse
        classes = [str(x) for x in (cl.get("data_classes") or DATA_CLASSES_DEFAULT)]
        for key in ("default_data_class", "max_cloud_data_class"):
            if cl.get(key, _d(f"router.cloud.{key}")) not in classes:
                return invalid(f"router.cloud.{key} must be one of the data classes {classes}")
        scan = cl.get("credential_scan", _d("router.cloud.credential_scan"))
        if scan not in ("block", "off"):
            return invalid("router.cloud.credential_scan must be block or off")
        allow = {"api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com"} | {str(h) for h in (cl.get("egress_allow") or [])}
        providers = {}
        for pname, spec in (cl.get("providers") or {}).items():
            spec = dict(spec or {})
            if spec.get("kind") not in ("openai", "anthropic"):
                return invalid(f"router.cloud.providers.{pname}.kind must be openai or anthropic")
            host = urlparse(str(spec.get("base_url") or "")).hostname
            if not host or host not in allow:
                return invalid(f"router.cloud.providers.{pname}.base_url: host {host!r} not in the egress allowlist {sorted(allow)}")
            if not spec.get("api_key_env"):
                return invalid(f"router.cloud.providers.{pname}.api_key_env missing")
            if spec.get("max_data_class") is not None and spec["max_data_class"] not in classes:
                return invalid(f"router.cloud.providers.{pname}.max_data_class must be one of the data classes {classes}")
            spec["enabled"] = bool(spec.get("enabled", True))
            providers[str(pname)] = spec
        privacy = parse_privacy_config(cl.get("privacy_check"))
        if failed(privacy):
            return privacy
        self.cloud = {"enabled": bool(cl.get("enabled", _d("router.cloud.enabled"))), "data_classes": classes,
                      "default_data_class": cl.get("default_data_class", _d("router.cloud.default_data_class")),
                      "max_cloud_data_class": cl.get("max_cloud_data_class", _d("router.cloud.max_cloud_data_class")),
                      "credential_scan": scan, "egress_allow": sorted(allow), "providers": providers, "privacy_check": privacy}
        for cname, cspec in self.client_auth["clients"].items():
            if cspec.get("data_class") is not None and cspec["data_class"] not in classes:
                return invalid(f"client_auth.clients.{cname}.data_class must be one of the data classes {classes}")
            if cspec.get("privacy_check") is not None and type(cspec["privacy_check"]) is not bool:
                return invalid(f"client_auth.clients.{cname}.privacy_check must be boolean")
            if cspec.get("privacy_check") is True and not privacy.enabled:
                return invalid(f"client_auth.clients.{cname}.privacy_check needs router.cloud.privacy_check.enabled: true")

    def _parse_wol(self, w):
        self.wol_wait_s = float(w.get("wait_up_s", _d("router.wol.wait_up_s")))
        self.wol_retry_s = float(w.get("retry_interval_s", _d("router.wol.retry_interval_s")))
        self.wol_cooldown_s = float(w.get("cooldown_s", _d("router.wol.cooldown_s")))
        self.wol_broadcasts = list(w.get("broadcast_addresses", ["255.255.255.255"]))

    def _parse_modes(self, m):
        be = m.get("busy_enter") or {}
        self.busy_util = float(be.get("gpu_util_pct", _d("modes.busy_enter.gpu_util_pct")))
        self.busy_sustain = float(be.get("sustain_s", _d("modes.busy_enter.sustain_s")))
        self.busy_foreign_gib = float(be.get("or_foreign_vram_gib", _d("modes.busy_enter.or_foreign_vram_gib")))
        self.foreign_sustain = float(be.get("foreign_sustain_s", _d("modes.busy_enter.foreign_sustain_s")))
        self.util_requires_foreign_gib = float(be.get("util_requires_foreign_gib", _d("modes.busy_enter.util_requires_foreign_gib")))
        self.busy_exit_s = float((m.get("busy_exit") or {}).get("below_for_s", _d("modes.busy_exit.below_for_s")))
        # Hysteresis (0.6.8): once busy, the node stays busy as long as utilization is above this threshold AND foreign
        # VRAM is occupied (util_requires_foreign_gib) - otherwise a game with 31-44% load would flap around the entry threshold
        self.busy_exit_util = float((m.get("busy_exit") or {}).get("gpu_util_pct", _d("modes.busy_exit.gpu_util_pct")))
        # Agent >= 0.17.0 reports game and main memory (Router 0.7.0): a game makes it busy (after game_sustain_s), then
        # waits for free game_quiet_s; memory pressure (RAM or Commit free below min_free_gib, polls Heartbeats in a row) blocks
        # cold loading and prewarming, but evicts nothing
        self.busy_game = bool(be.get("game", _d("modes.busy_enter.game")))
        self.game_sustain = float(be.get("game_sustain_s", _d("modes.busy_enter.game_sustain_s")))
        # 0.8.7 / Agent 0.19.0: if the agent reports the game's GPU demand, it only becomes busy if the game itself requires that much
        # graphics memory (own + shared) or engine utilization - or under memory pressure. Without reporting, as before.
        self.game_min_gpu_gib = float(be.get("game_min_gpu_gib", _d("modes.busy_enter.game_min_gpu_gib")))
        self.game_min_gpu_pct = float(be.get("game_min_gpu_pct", _d("modes.busy_enter.game_min_gpu_pct")))
        self.game_exit_s = float((m.get("busy_exit") or {}).get("game_quiet_s", _d("modes.busy_exit.game_quiet_s")))
        mp = m.get("memory_pressure") or {}
        self.memory_pressure = {"min_free_gib": float(mp.get("min_free_gib", _d("modes.memory_pressure.min_free_gib"))),
                                "polls": int(mp.get("polls", _d("modes.memory_pressure.polls")))}
        reserve = m.get("vram_reserve_gib") or {"free": _d("modes.vram_reserve_gib.free"), "busy": _d("modes.vram_reserve_gib.busy")}
        self.reserve = {k: float(v) for k, v in reserve.items()}
        self.keep_alive = m.get("keep_alive") or {"free": _d("modes.keep_alive.free"), "busy": _d("modes.keep_alive.busy")}
        self.unload_on_busy = bool(m.get("unload_on_busy", _d("modes.unload_on_busy")))
        self.unload_on_busy_interval_s = float(m.get("unload_on_busy_interval_s", _d("modes.unload_on_busy_interval_s")))   # Minimum interval of the safety net
        self.vram_settle_s = float(m.get("vram_settle_s", _d("modes.vram_settle_s")))   # Lag until nvidia-smi has tracked an unload
        self.overhead_gib = float(m.get("fit_overhead_gib", _d("modes.fit_overhead_gib")))
        # warm first: an already loaded model from the tier list beats a cold start of a higher tier
        self.warm_first = bool(m.get("warm_first", _d("modes.warm_first")))
        # Prewarming of rank-1 models so the list doesn't stay stuck on the small model after busy
        pw = m.get("prewarm") or {}
        self.prewarm_on_free = bool(pw.get("on_free", _d("modes.prewarm.on_free")))
        self.prewarm_on_online = bool(pw.get("on_online", _d("modes.prewarm.on_online")))
        self.prewarm_free_delay = float(pw.get("free_delay_s", _d("modes.prewarm.free_delay_s")))
        self.prewarm_online_delay = float(pw.get("online_delay_s", _d("modes.prewarm.online_delay_s")))
        # Residency: if a specifically requested foreign model evicted the rank-1 model and hasn't been used for residency_idle_s,
        # rank-1 is prewarmed again. Without this, "warm first" would permanently prefer the fallback tier on
        # ANOTHER node (2026-09-10: glm evicted qwen, everything ran on gpu-laptop/gemma4:12b). 0 = off.
        self.residency_idle_s = float(pw.get("residency_idle_s", _d("modes.prewarm.residency_idle_s")))
        self.residency_check_s = float(pw.get("residency_check_s", _d("modes.prewarm.residency_check_s")))
        # Stage 3 Scheduler: weighted score (scheduler.score), Circuit Breaker per node, Admission Control
        self.score = {k: float(_d(f"modes.score.{k}")) for k in settings_mod.SCORE_KEYS}
        for k, v in (m.get("score") or {}).items():
            if k not in self.score:
                return invalid(f"modes.score.{k}: unknown weight (known: {', '.join(self.score)})")
            self.score[k] = float(v)
        br = m.get("breaker") or {}
        self.breaker = {"failures": int(br.get("failures", _d("modes.breaker.failures"))), "window_s": float(br.get("window_s", _d("modes.breaker.window_s"))),
                        "open_s": float(br.get("open_s", _d("modes.breaker.open_s")))}
        ad = m.get("admission") or {}
        self.admission = {"max_inflight_default": int(ad.get("max_inflight_default", _d("modes.admission.max_inflight_default"))),
                          "aging_s": float(ad.get("aging_s", _d("modes.admission.aging_s"))),
                          "max_wait_s": float(ad.get("max_wait_s", _d("modes.admission.max_wait_s"))), "max_queue": int(ad.get("max_queue", _d("modes.admission.max_queue"))),
                          # Wait budget per class (admission.max_wait_s): max_wait_s applies to normal
                          "max_wait_interactive_s": float(ad.get("max_wait_interactive_s", _d("modes.admission.max_wait_interactive_s"))),
                          "max_wait_batch_s": float(ad.get("max_wait_batch_s", _d("modes.admission.max_wait_batch_s")))}
        # GPU guard (design/gpu-guard.md): the agent sets the power limit, the router reacts to its status -
        # Stage 2 (throttled) caps parallelism, high load costs score, problems (unavailable, rejected,
        # voltage, temperature) go to HA. require_fresh_status: cap even without fresh status (costs
        # parallelism on every agent failure, hence default off).
        gg = m.get("gpu_guard") or {}
        self.gpu_guard = {"enabled": bool(gg.get("enabled", _d("modes.gpu_guard.enabled"))),
                          "throttled_max_inflight": int(gg.get("throttled_max_inflight", _d("modes.gpu_guard.throttled_max_inflight"))),
                          "score_penalty": float(gg.get("score_penalty", _d("modes.gpu_guard.score_penalty"))),
                          "require_fresh_status": bool(gg.get("require_fresh_status", _d("modes.gpu_guard.require_fresh_status")))}
        # Agent update via router (agentupdate.py): Rollout loop on/off, canary node, wait time, optional
        # Public operator key for self-checking the manifest before sending.
        au = m.get("agent_update") or {}
        self.agent_update = {"enabled": bool(au.get("enabled", _d("modes.agent_update.enabled"))), "canary": au.get("canary") or "",
                             "canary_clean_h": float(au.get("canary_clean_h", _d("modes.agent_update.canary_clean_h"))), "public_key": au.get("public_key") or ""}
        # Ollama update via router (ollamaupdate.py, 0.3.0): version check on GitHub, rollout in night window,
        # Canary (empty = same as agent update).
        ou = m.get("ollama_update") or {}
        self.ollama_update = {"enabled": bool(ou.get("enabled", _d("modes.ollama_update.enabled"))), "canary": ou.get("canary") or "",
                              "canary_clean_h": float(ou.get("canary_clean_h", _d("modes.ollama_update.canary_clean_h"))),
                              "window_start": str(ou.get("window_start", _d("modes.ollama_update.window_start")) or ""),
                              "window_end": str(ou.get("window_end", _d("modes.ollama_update.window_end")) or ""),
                              "timezone": str(ou.get("timezone", _d("modes.ollama_update.timezone")) or ""),
                              "check_interval_h": float(ou.get("check_interval_h", _d("modes.ollama_update.check_interval_h"))),
                              "release_url": str(ou.get("release_url", _d("modes.ollama_update.release_url")) or "")}
        if self.ollama_update["timezone"]:
            import zoneinfo
            # Only check where a timezone database exists: the local schema check in deploy.py runs on Windows Python
            # without tzdata and would otherwise reject every zone (2026-09-27)
            if zoneinfo.available_timezones() and self.ollama_update["timezone"] not in zoneinfo.available_timezones():
                return invalid(f"modes.ollama_update.timezone: unknown time zone {self.ollama_update['timezone']!r}")
        for k in ("window_start", "window_end"):
            v = self.ollama_update[k]
            if v and not re.match(r"^\d{2}:\d{2}$", v):
                return invalid(f"modes.ollama_update.{k}: HH:MM expected, not {v!r}")
        # Router 0.16.0: OpenAI-compatible servers (Strata) only in a window or on request (poll.upstream_window_check)
        uw = m.get("upstream_window") or {}
        self.upstream_window = {"enabled": bool(uw.get("enabled", _d("modes.upstream_window.enabled"))),
                                "window_start": str(uw.get("window_start", _d("modes.upstream_window.window_start")) or ""),
                                "window_end": str(uw.get("window_end", _d("modes.upstream_window.window_end")) or ""),
                                "timezone": str(uw.get("timezone", _d("modes.upstream_window.timezone")) or ""),
                                "idle_hold_s": float(uw.get("idle_hold_s", _d("modes.upstream_window.idle_hold_s")))}
        if self.upstream_window["timezone"]:
            import zoneinfo
            if zoneinfo.available_timezones() and self.upstream_window["timezone"] not in zoneinfo.available_timezones():
                return invalid(f"modes.upstream_window.timezone: unknown time zone {self.upstream_window['timezone']!r}")
        for k in ("window_start", "window_end"):
            v = self.upstream_window[k]
            if not re.match(r"^\d{2}:\d{2}$", v):
                return invalid(f"modes.upstream_window.{k}: HH:MM expected, not {v!r}")

    def _parse_models(self, models):
        self.models = models
        for mname, spec in self.models.items():
            caps = (spec or {}).get("capabilities")
            if caps is not None and (not isinstance(caps, dict) or not all(isinstance(v, bool) for v in caps.values())):
                return invalid(f"models.{mname}.capabilities must be a mapping capability -> true/false")
            if (spec or {}).get("cloud"):
                if spec["cloud"] not in self.cloud["providers"]:
                    return invalid(f"models.{mname}.cloud: provider {spec['cloud']} is not configured under router.cloud.providers")
            elif (spec or {}).get("weights_gib") is None:
                return invalid(f"models.{mname}: weights_gib missing (local model) or cloud: <provider> (cloud model)")

    def _parse_roles(self, roles):
        self.roles = {}
        for rname, r in roles.items():
            exposed = r.get("exposed_as", f"{rname}:latest")
            tiers = []
            for t in r["tiers"]:
                if t["model"] not in self.models:
                    return invalid(f"role {rname}: tier model {t['model']} missing from the model catalog")
                tier = {"model": t["model"], "num_ctx": int(t["num_ctx"]), "busy_ok": bool(t.get("busy_ok", False))}
                if (self.models[t["model"]] or {}).get("cloud"):
                    tier["cloud"] = self.models[t["model"]]["cloud"]   # Stage 5: Cloud tier
                tiers.append(tier)
            lf = r.get("latency_first")   # None = globale Einstellung warm_first
            prio = r.get("priority", "normal")   # Stage 3: Standard class of the role (routing.priority overrides)
            if prio not in PRIORITIES:
                return invalid(f"role {rname}: priority must be one of {PRIORITIES}")
            role = {"name": rname, "exposed": exposed, "tiers": tiers,
                    "latency_first": self.warm_first if lf is None else bool(lf),
                    "latency_first_override": lf, "priority": prio}
            for kind in ("canary", "shadow"):   # Stage 6: Measure candidate model with real traffic
                spec = r.get(kind)
                if spec:
                    if spec.get("model") not in self.models:
                        return invalid(f"role {rname}: {kind} model {spec.get('model')} missing from the model catalog")
                    pct = float(spec.get("percent", 0))
                    if not 0 <= pct <= 100:
                        return invalid(f"role {rname}: {kind}.percent must be between 0 and 100")
                    role[kind] = {"model": spec["model"], "num_ctx": int(spec.get("num_ctx", tiers[0]["num_ctx"])), "percent": pct,
                                  "busy_ok": bool(spec.get("busy_ok", False))}
            self.roles[exposed] = role

    def _parse_decision(self, de):
        """Check decision_engine: options are roles (short name), the auto role must not be a real role, the chain
        only knows known engines and jevlike needs an endpoint."""
        out = dict(de)
        if not de.get("enabled"):
            out["enabled"] = False
            return out
        role_names = {r["name"] for r in self.roles.values()}
        role = str(de.get("role") or "auto")
        if role in role_names or f"{role}:latest" in self.roles:
            return invalid(f"decision_engine.role {role!r} is already a role")
        options = [str(o) for o in (de.get("options") or [])]
        unknown = [o for o in options if o not in role_names]
        if len(options) < 2 or unknown:
            return invalid(f"decision_engine.options: at least two roles, unknown: {unknown}")
        default = str(de.get("default") or options[0])
        if default not in options:
            return invalid(f"decision_engine.default {default!r} is not in options")
        chain = [str(e) for e in (de.get("chain") or ["rules"])]
        bad = [e for e in chain if e not in DECISION_ENGINES]
        if bad:
            return invalid(f"decision_engine.chain: unknown engine {bad} (allowed: {DECISION_ENGINES})")
        if "jevlike" in chain and not (de.get("jevlike") or {}).get("endpoint"):
            return invalid("decision_engine.jevlike.endpoint missing, but jevlike is in the chain")
        if "local_llm" in chain and not (de.get("local_llm") or {}).get("model"):
            return invalid("decision_engine.local_llm.model missing, but local_llm is in the chain")
        light = [str(o) for o in (de.get("observe_light") or [])]
        if [o for o in light if o not in options]:
            return invalid(f"decision_engine.observe_light: only options of the engine ({options}), not {light}")
        if "embed" in chain and not (de.get("embed") or {}).get("endpoint"):
            return invalid("decision_engine.embed.endpoint missing, but embed is in the chain")
        if "tfidf" in chain and not (de.get("tfidf") or {}).get("model_path"):
            return invalid("decision_engine.tfidf.model_path missing, but tfidf is in the chain")
        for k, lo, hi in (("min_top_probability", 0, 1), ("min_margin", 0, 1), ("max_entropy_ratio", 0, 1)):
            v = (de.get("policy") or {}).get(k)
            if v is not None and not lo <= float(v) <= hi:
                return invalid(f"decision_engine.policy.{k} must be between {lo} and {hi}")
        out.update({"enabled": True, "role": role, "options": options, "default": default, "chain": chain})
        return out

    def catalog_twin(self, model):
        """Catalog entry for a name: direct or via same digest (alias like `mein-assistent`)."""
        if model in self.models:
            return self.models[model]
        for n in state.NODES.values():
            d = n.digest_of.get(model)
            if d is None:
                continue
            for cm in self.models:
                if n.digest_of.get(cm) == d:
                    return self.models[cm]
        return None

    def real_gib(self, model, ctx):
        """Real usage (without fit bonus) of a loaded model at `ctx`, if the catalog measured it
        (`real: true`, since 0.3.5), else None. /api/ps reports only part: qwen3.6:35b-a3b at 196k 21.0 GiB,
        nvidia-smi shows 26.4 GiB more than with empty Ollama (measured 2026-09-30). Without this number the
        router underestimated loaded models - the rest fell into desktop baseline or was treated as foreign VRAM."""
        m = self.catalog_twin(model)
        if m is None or m.get("cloud") or not m.get("real") or not ctx:
            return None
        return float(m["weights_gib"]) + float(m.get("kv_gib_per_1k", 0)) * ctx / 1000.0

    def max_parallel(self, model):
        """Catalog `max_parallel` (also via digest twin of an alias) or None = no limit per model."""
        m = self.catalog_twin(model)
        v = m.get("max_parallel") if m is not None else None
        return int(v) if v else None

    def need_gib(self, model, ctx, node=None):
        m = self.catalog_twin(model)
        if m is not None and m.get("cloud"):
            return 0.0
        if m is not None:
            # Measured via nvidia-smi (real, since 0.3.5): this is the total requirement. The fit bonus only compensated for
            # small /api/ps numbers; safety is provided by the reserve in the budget (modes.vram_reserve_gib).
            extra = 0.0 if m.get("real") else self.overhead_gib
            return float(m["weights_gib"]) + float(m.get("kv_gib_per_1k", 0)) * ctx / 1000.0 + extra
        if node is not None and node.is_loaded(model):
            return node.loaded_size(model) + 0.5          # Already resident, fits by definition
        # Unknown and cold: file size from /api/tags as weights, KV unknown -> 20% surcharge
        size = max((n.model_details.get(model, {}).get("size", 0) for n in state.NODES.values()), default=0) / GIB
        return size * 1.2 + self.overhead_gib


def cert_fingerprint(pem_path):
    """SHA-256 (hex) over the DER certificate of a PEM file."""
    with open(pem_path, encoding="utf-8") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).hexdigest()


def write_overrides(new):
    """Validate roles.yaml (probe config) and write atomically, then reload live. None = written and active; a
    Problem 400 = invalid (nothing written), 500 = file not writable (nothing changed)."""
    probe = Config.__new__(Config)
    probe._locate(state.CFG.path, True)
    merged = probe.merged(new)
    if failed(merged):
        return merged
    if failed(problem := probe.parse(merged)):
        return problem
    tmp = state.CFG.overrides_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# Written by the router UI (%s). Overrides roles/models/expose_concrete_models from config.yaml;\n"
                    "# settings = single settings (path -> value, allowlist in settings.py), delete_models = catalog entries removed in the UI.\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S"))
            yaml.safe_dump(new, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, state.CFG.overrides_path)
    except OSError as e:  # exception-ok: file system (disk full, permissions) - only the write itself can tell
        return Problem(500, f"roles.yaml not written: {type(e).__name__}")
    if failed(problem := state.CFG.reload()):   # The probe was valid - here only in case config.yaml changed in between
        return problem
    from . import cloud, decision
    cloud.setup()
    decision.ensure()
    return None


def read_overrides():
    """roles.yaml parsed (cache by mtime). Callers modifying the tree must copy."""
    import copy
    return copy.deepcopy(load_yaml(state.CFG.overrides_path, {}) or {})


def settings_view(lang="de"):
    ov = read_overrides()
    return settings_mod.view(state.CFG, state.CFG.base(), ov.get("settings") or {}, lang=lang)


def roles_as_config():
    return {r["name"]: {"exposed_as": r["exposed"], "latency_first": r["latency_first_override"],
                        "latency_first_effective": r["latency_first"], "priority": r["priority"],
                        "canary": r.get("canary"), "shadow": r.get("shadow"),
                        "tiers": [dict(t) for t in r["tiers"]]} for r in state.CFG.roles.values()}
