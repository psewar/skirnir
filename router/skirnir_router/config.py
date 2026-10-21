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
from . import settings as settings_mod
from .common import DATA_CLASSES_DEFAULT, GIB, PRIORITIES, THINK_MODES, log
from .outcome import Problem, failed, invalid


def _d(path):
    """Laufzeit-Default eines Schluessels aus settings.EDITABLE - eine Quelle fuer Parser, UI und Anzeige der Basiswerte."""
    return settings_mod.EDITABLE[path]["default"]


# Performance (2026-09-11): yaml.safe_load ist der reine Python-Parser (config.yaml 15,6 ms auf Router-CT), der libyaml-Loader
# braucht 1,5 ms. Dazu ein Cache nach Dateizeit: /admin/config las config.yaml und roles.yaml je zweimal pro Aufruf (~86 ms),
# und die UI fragt das alle paar Sekunden ab. Schreibpfade (write_overrides) invalidieren ueber die geaenderte mtime.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_YAML_CACHE = {}   # pfad -> (mtime_ns, size, geparster Baum)


def load_yaml(path, default=None):
    """YAML-Datei geparst zurueckgeben - aus dem Cache, solange sich mtime/Groesse nicht geaendert haben. Liefert eine
    tiefe Kopie? Nein: Aufrufer duerfen den Baum NICHT veraendern (merged() arbeitet auf einer eigenen Kopie)."""
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

# Stufe 6: Konfigurationsschema. dict = erlaubte Unterschluessel, None = Blatt (Wert beliebig, Typpruefung in parse),
# "*" = beliebige Namen. Unbekannte Schluessel sind ein Fehler (Tippfehler wie `limitz` wurden bisher stumm ignoriert).
_TIER = {"model": None, "num_ctx": None, "busy_ok": None}
_CANARY = {"model": None, "num_ctx": None, "percent": None, "busy_ok": None}
# aus den Feldern, die parse_client_memory annimmt - eine Quelle, sonst verweigert das Schema einen gueltigen Schluessel
_MEMORY_SCHEMA = {**dict.fromkeys(CLIENT_MEMORY_FIELDS), "projects": {"*": dict.fromkeys(PROJECT_FIELDS)}}
SCHEMA = {
    "router": {
        "listen": None, "control_listen": None, "public_url": None, "control_tls": {"cert": None, "key": None},
        "ollama_poll_s": None, "heartbeat_interval_s": None, "heartbeat_stale_s": None, "offline_after_misses": None,
        "agent_missing_problem_s": None, "expose_concrete_models": None, "request_timeout_s": None,
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
                                          "cloud": None, "data_class": None, "rerank": None, "inference": None, "session_projects": None, "session_handoff_from": None, "memory": _MEMORY_SCHEMA, "note": None, "token_rotated": None, "created": None}}},
        "cloud": {"enabled": None, "data_classes": None, "default_data_class": None, "max_cloud_data_class": None, "credential_scan": None,
                  "egress_allow": None,
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
    },
    "models": {"*": {"weights_gib": None, "kv_gib_per_1k": None, "capabilities": None, "source": None, "note": None, "measured_at": None,
                     "measured_on": None, "vram_gib_8k": None, "vram_gib_32k": None, "partial_offload": None,
                     # seit 0.3.5: real = weights/kv aus dem nvidia-smi-Zuwachs (nicht aus /api/ps), je Kontext beide Werte
                     "real": None, "vram_real_gib": None, "vram_ps_gib": None,
                     "cloud": None, "provider_model": None, "price_chf_per_m": None, "context_tokens": None, "reasoning": None,
                     # seit 0.6.5: hoechstens so viele Anfragen gleichzeitig je Knoten (qwen3.8: 1, Ollama kann qwen35 nicht parallel)
                     "max_parallel": None}},
    "nodes": {"*": {"ollama": None, "vram_total_gib": None, "wol": None, "weight": None, "mac": None, "foreign_vram_baseline_gib": None,
                    "gpu": None, "ollama_tls_fingerprint": None, "ollama_tls_cert": None, "max_inflight": None}},
    "roles": {"*": {"exposed_as": None, "tiers": [_TIER], "latency_first": None, "priority": None, "canary": _CANARY, "shadow": _CANARY}},
    # Decision Engine (2026-09-16): Auto-Rolle, deren Rolle eine Kette von Engines waehlt (decision/__init__.py)
    "decision_engine": {"enabled": None, "role": None, "options": None, "default": None, "chain": None, "context_chars": None,
                        "policy": {"min_top_probability": None, "min_margin": None, "max_entropy_ratio": None},
                        "jevlike": {"endpoint": None, "model": None, "timeout_s": None},
                        "local_llm": {"model": None, "timeout_s": None, "descriptions": None},
                        "tfidf": {"model_path": None},
                        "embed": {"endpoint": None, "model": None, "timeout_s": None},
                        "rules": None, "calibration_path": None,
                        "capture": {"enabled": None, "path": None, "clients": None, "anonymize": None, "max_context_chars": None},
                        # 0.6.7: Beobachtungsmodus - wartet eine Anfrage, klassifizieren und protokollieren, ob eine tiefere freie
                        # Stufe gereicht haette (ausweich_beobachtung.py); observe_light = Klassen, fuer die das in Frage kaeme
                        "observe_blocked": None, "observe_light": None,
                        # ... und observe_all: JEDE Anfrage im Hintergrund einordnen, Zeile je Anfrage in observe_path
                        # (Vorgabe anfragen.jsonl neben config.yaml) - Klasse, Dauer, Token, keine Inhalte
                        "observe_all": None, "observe_path": None},
}
DECISION_ENGINES = ("rules", "jevlike", "local_llm", "tfidf", "embed")


def validate_schema(c, schema=SCHEMA, path=""):
    """Unbekannte Schluessel benennen (mit Vorschlag), Abschnitte muessen Mappings sein. Liefert Fehlerliste."""
    errs = []
    if not isinstance(c, dict):
        return [f"{path or '<root>'}: muss ein Mapping sein"]
    for k, v in c.items():
        sub = schema.get(k, schema.get("*", "MISSING"))
        here = f"{path}.{k}" if path else str(k)
        if sub == "MISSING":
            hint = difflib.get_close_matches(str(k), [s for s in schema if s != "*"], n=1, cutoff=0.6)
            errs.append(f"unbekannter Schluessel {here}" + (f" - meinten Sie '{hint[0]}'?" if hint else "") +
                        f" (erlaubt: {', '.join(sorted(s for s in schema if s != '*'))})")
            continue
        if isinstance(sub, dict) and v is not None:
            errs += validate_schema(v, sub, here)
        elif isinstance(sub, list) and v is not None:
            if not isinstance(v, list):
                errs.append(f"{here}: muss eine Liste sein")
            else:
                for i, item in enumerate(v):
                    errs += validate_schema(item, sub[0], f"{here}[{i}]")
    return errs


# Groessenangaben eines Katalogeintrags gehoeren zusammen: eine Messung (UI, perf) liefert sie als Einheit, und neue Gewichte mit
# einer alten Messung (real, vram_real_gib) zu mischen hiesse, die alte Messung fuer die neuen Werte auszugeben.
SIZING_FIELDS = ("weights_gib", "kv_gib_per_1k", "real", "vram_real_gib", "vram_ps_gib", "vram_gib_8k", "vram_gib_32k",
                 "measured_at", "measured_on", "partial_offload")


def merge_models(base, overlay):
    """Katalog aus config.yaml + roles.yaml, je Modell Feld fuer Feld (0.6.9). Bis 0.6.8 ersetzte ein roles.yaml-Eintrag den aus
    config.yaml ganz - max_parallel aus config.yaml galt fuer qwen3.8 nicht, weil roles.yaml eine Messung des Modells hielt
    (2026-10-02). Bringt der Overlay-Eintrag Groessenangaben mit, ersetzen sie die der Basis als Einheit (SIZING_FIELDS);
    alle anderen Felder (max_parallel, capabilities, note, ...) bleiben aus der Basis, wenn der Overlay sie nicht setzt."""
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
    """config.yaml (kommentiert, per deploy) + roles.yaml (von der UI geschrieben, überschreibt roles/models/expose)."""

    def __init__(self, path, use_overrides=True):
        """Fuer Aufrufer ohne Problem-Pfad (Tests, Werkzeuge). Config.load() ist die ausdrueckliche Form und liefert eine
        ungueltige Konfiguration als Problem."""
        self._locate(path, use_overrides)
        problem = self.reload()
        if failed(problem):
            raise ValueError(problem.message)  # exception-ok: a constructor has no return channel; Config.load returns it

    @classmethod
    def load(cls, path, use_overrides=True):
        """Config oder ein Problem 400 (ungueltige Konfiguration) - Start, --check und Neuladen entscheiden daran."""
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
                # Die UI schreibt nur Stufen/latency_first. Alles andere an einer Rolle (priority, canary, shadow) bleibt aus
                # config.yaml erhalten - sonst verlor `assist` seine Prioritaet, sobald roles.yaml existierte (2026-09-10).
                base = c.get("roles") or {}
                c["roles"] = {name: {**(base.get(name) or {}), **(r or {})} for name, r in (v or {}).items()}
            elif k == "models":
                c["models"] = merge_models(c.get("models") or {}, v or {})
            elif k == "expose_concrete_models":
                c["router"]["expose_concrete_models"] = bool(v)
        # Clients aus der UI (neue Clients, rotierte Secrets): je Client ueber den config.yaml-Eintrag gelegt, Felder einzeln
        if (overrides or {}).get("clients"):
            ca = c.setdefault("router", {}).setdefault("client_auth", None) or {}
            c["router"]["client_auth"] = ca
            cl = ca.get("clients") or {}
            ca["clients"] = cl
            for name, spec in (overrides["clients"] or {}).items():
                cl[str(name)] = {**(cl.get(name) or {}), **(spec or {})}
        # Stufe 7 (UI-Einstellungen): settings {pfad: wert} zuletzt - Rollen aus roles.yaml sind dann bekannt (Client-Rollen)
        if (overrides or {}).get("settings"):
            applied = settings_mod.apply(c, overrides["settings"])
            if failed(applied):
                return applied
        return c

    def base(self):
        """config.yaml pur (ohne roles.yaml) - fuer die Anzeige der Basiswerte in der UI. Nur lesen, nicht veraendern (Cache)."""
        return load_yaml(self.path)

    def reload(self, overrides=None):
        """Neu laden; None = uebernommen, ein Problem = ungueltig und NICHTS geaendert. Erst eine Probe, dann dieses Objekt:
        bis 0.8.9 brach parse() mitten im Ueberschreiben ab, und "config reload failed, keeping old" behielt eine halb neue
        Konfiguration."""
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
        """Baum c pruefen und uebernehmen. None = ok, ein Problem = ungueltig (dann kann das Objekt teilweise belegt sein -
        darum prueft reload() erst an einer Probe)."""
        errs = validate_schema(c)
        if errs:
            return invalid("Konfiguration: " + "; ".join(errs))
        for sect in ("router", "modes", "roles"):
            if not isinstance(c.get(sect), dict):
                return invalid(f"Konfiguration: Abschnitt {sect} fehlt")
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
                            ("rerank", parse_rerank_config(r.get("rerank"))),   # POST /rerank (rerank.py), default aus
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
        self.hb_interval_s = float(r.get("heartbeat_interval_s", _d("router.heartbeat_interval_s")))   # Takt der Agenten (wird provisioniert)
        self.hb_stale_s = float(r.get("heartbeat_stale_s", _d("router.heartbeat_stale_s")))
        if self.hb_stale_s < 2 * self.hb_interval_s:   # sonst gilt ein Knoten zwischen zwei Meldungen als "GPU unbekannt"
            self.hb_stale_s = 2 * self.hb_interval_s + 1
        self.agent_missing_s = float(r.get("agent_missing_problem_s", _d("router.agent_missing_problem_s")))   # Agent war da und schweigt -> HA-Problem
        self.expose_concrete = bool(r.get("expose_concrete_models", False))
        self.request_timeout_s = float(r.get("request_timeout_s", _d("router.request_timeout_s")))
        self.heartbeat_token = r.get("heartbeat_token") or ""
        self.api_tls = bool(r.get("api_tls", False))   # 11434 ebenfalls TLS (Zertifikat aus control_tls)
        # Im Text gestrandete Tool-Calls zurueckholen (siehe toolcall_rescue). Standard an:
        # die Stufe greift nur, wenn Ollama selbst nichts geparst hat, und laesst sonst alles unberuehrt.
        self.toolcall_rescue = bool(r.get("toolcall_rescue", _d("router.toolcall_rescue")))
        # OpenAI-kompatible Schnittstelle /v1 (Node-RED-MCP, andere OpenAI-Clients). Der Router uebersetzt selbst nach
        # /api/chat, weil Ollamas eigenes /v1 kein num_ctx/keep_alive kennt (Modell wuerde mit Default-Kontext neu laden).
        oa = r.get("openai") or {}
        self.openai_enabled = bool(oa.get("enabled", True))
        # think, wenn der Client nichts sagt: false = Antwort ohne unsichtbares Nachdenken (wie HAs AI-Tasks);
        # true = Modell-Default; null = Feld nicht setzen (Ollama entscheidet)
        self.openai_default_think = oa.get("default_think", _d("router.openai.default_think"))
        # Anthropic-Messages-API /v1/messages (Claude Code). Ping-Abstand, solange der Knoten noch am Prompt rechnet:
        # Ollama schickt die Kopfzeilen erst mit dem ersten Token, bei 196k Kontext bis ~2 min.
        an = r.get("anthropic") or {}
        self.anthropic_enabled = bool(an.get("enabled", True))
        self.anthropic_keepalive_s = float(an.get("keepalive_s", _d("router.anthropic.keepalive_s")))
        # Denken: auto = wie der Client es verlangt (thinking-Block), on/off = fest. Claude Code verlangt bei jedem Zug
        # adaptives Denken; qwen3.8 kennt nur an/aus und denkt dann immer.
        self.anthropic_think = str(an.get("think", _d("router.anthropic.think")))
        if self.anthropic_think not in THINK_MODES:
            return invalid(f"router.anthropic.think muss eine von {THINK_MODES} sein, nicht {self.anthropic_think!r}")
        # Basic Auth für UI + /admin/*: {user: "pbkdf2:<iter>:<salt_hex>:<hash_hex>"}; leer = offen (nur Tests)
        self.control_users = dict((r.get("control_auth") or {}).get("users") or {})
        # TLS für den Control-Port: {cert: <fullchain.pem>, key: <key.pem>}; fehlt der Block -> Klartext (nur Tests)
        tls = r.get("control_tls") or {}
        self.control_tls = (tls.get("cert"), tls.get("key")) if tls.get("cert") and tls.get("key") else None
        self.public_url = r.get("public_url", "")
        # Stufe 1: Request-Groessenlimits (413 vor dem Backend) und Verfall der Session-Affinitaet
        lim = r.get("limits") or {}
        self.limits = {k: int(lim.get(k, _d(f"router.limits.{k}"))) for k in ("max_images", "max_tools", "max_messages")}
        self.session_ttl_s = float(r.get("session_affinity_ttl_s", _d("router.session_affinity_ttl_s")))
        self.idempotency_ttl_s = float(r.get("idempotency_ttl_s", _d("router.idempotency_ttl_s")))   # Stufe 6: Wiederholungen mit Idempotency-Key

    def _parse_client_auth(self, ca):
        # Client-Authentifizierung auf dem API-Port 11434 (Stufe 2, design/roadmap.md). mode: observe = alles bedienen,
        # Unbekannte zaehlen und ins Audit-Log; enforce = 401 ohne gueltige Identitaet. clients: {name: {token_sha256,
        # ip: [..], roles: [..]|["*"], models: bool, requests_per_minute}}. Kein clients-Block = Port offen wie bisher.
        mode = ca.get("mode", _d("router.client_auth.mode"))
        if mode not in ("observe", "enforce"):
            return invalid(f"client_auth.mode muss observe oder enforce sein, nicht {mode!r}")
        clients = {}
        for name, spec in (ca.get("clients") or {}).items():
            spec = dict(spec or {})
            th = (spec.get("token_sha256") or "").strip().lower()
            if th and (len(th) != 64 or any(ch not in "0123456789abcdef" for ch in th)):
                return invalid(f"client_auth.clients.{name}.token_sha256 muss 64 Hex-Zeichen sein")
            projects = spec.get("session_projects", [])
            if not isinstance(projects, list) or any(not isinstance(p, str) or not p.strip() or len(p) > 128
                                                     or any(ord(ch) < 32 for ch in p) for p in projects):
                return invalid("client_auth session_projects must be a list of nonempty project names (max 128 characters)")
            spec["session_projects"] = projects
            spec["token_sha256"] = th
            spec["ip"] = [str(x) for x in (spec.get("ip") or [])]
            spec["roles"] = [str(x) for x in (spec.get("roles") or ["*"])]
            spec["models"] = bool(spec.get("models", True))
            # Recht auf POST /rerank (rerank.py), default nein. Erkennung auch per ip: Hindsights TEI-Client schickt keinen Header.
            if type(spec.get("rerank", False)) is not bool:
                return invalid(f"client_auth.clients.{name}.rerank muss true oder false sein")
            spec["rerank"] = spec.get("rerank", False)
            # inference: false = nur POST /rerank und GET /info (auth.inference_refusal); Standard true wie bisher
            if type(spec.get("inference", True)) is not bool:
                return invalid(f"client_auth.clients.{name}.inference muss true oder false sein")
            spec["inference"] = spec.get("inference", True)
            if spec["rerank"] and spec["inference"] and "*" in spec["roles"]:
                log.warning("client_auth.clients.%s: rerank: true ohne inference: false und ohne roles-Grenze - der Client darf "
                            "auch jede Rolle rufen", name)
            if spec.get("requests_per_minute") is not None:
                spec["requests_per_minute"] = int(spec["requests_per_minute"])
            if spec.get("max_priority") is not None and spec["max_priority"] not in PRIORITIES:   # Stufe 3: Kappung der Klasse
                return invalid(f"client_auth.clients.{name}.max_priority muss eine von {PRIORITIES} sein")
            clients[str(name)] = spec
        # locked (Audit 2026-09-16): true = der Modus ist NUR ueber config.yaml + Deploy aenderbar; UI-Einstellung und
        # POST /admin/client_auth werden abgelehnt (403/400 + Audit-Eintrag). Ein kompromittiertes UI-Konto kann die
        # Client-Auth dann nicht mehr per Klick auf observe stellen.
        self.client_auth = {"mode": mode, "clients": clients, "locked": bool(ca.get("locked", False)),
                            "audit_log": ca.get("audit_log", "/var/log/skirnir-router/audit.jsonl")}

    def _parse_cloud(self, cl):
        # Stufe 5: Cloud-Anbieter als Stufen in Rollen (cloud.py). Kein Opt-in pro Client: die Rolle erlaubt, der Client kann
        # sich per routing.execution: local oder clients.<c>.cloud: false ausnehmen.
        from urllib.parse import urlparse
        classes = [str(x) for x in (cl.get("data_classes") or DATA_CLASSES_DEFAULT)]
        for key in ("default_data_class", "max_cloud_data_class"):
            if cl.get(key, _d(f"router.cloud.{key}")) not in classes:
                return invalid(f"router.cloud.{key} muss eine der Datenklassen {classes} sein")
        scan = cl.get("credential_scan", _d("router.cloud.credential_scan"))
        if scan not in ("block", "off"):
            return invalid("router.cloud.credential_scan muss block oder off sein")
        allow = {"api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com"} | {str(h) for h in (cl.get("egress_allow") or [])}
        providers = {}
        for pname, spec in (cl.get("providers") or {}).items():
            spec = dict(spec or {})
            if spec.get("kind") not in ("openai", "anthropic"):
                return invalid(f"router.cloud.providers.{pname}.kind muss openai oder anthropic sein")
            host = urlparse(str(spec.get("base_url") or "")).hostname
            if not host or host not in allow:
                return invalid(f"router.cloud.providers.{pname}.base_url: Host {host!r} nicht in der Egress-Allowlist {sorted(allow)}")
            if not spec.get("api_key_env"):
                return invalid(f"router.cloud.providers.{pname}.api_key_env fehlt")
            if spec.get("max_data_class") is not None and spec["max_data_class"] not in classes:
                return invalid(f"router.cloud.providers.{pname}.max_data_class muss eine der Datenklassen {classes} sein")
            spec["enabled"] = bool(spec.get("enabled", True))
            providers[str(pname)] = spec
        self.cloud = {"enabled": bool(cl.get("enabled", _d("router.cloud.enabled"))), "data_classes": classes,
                      "default_data_class": cl.get("default_data_class", _d("router.cloud.default_data_class")),
                      "max_cloud_data_class": cl.get("max_cloud_data_class", _d("router.cloud.max_cloud_data_class")),
                      "credential_scan": scan, "egress_allow": sorted(allow), "providers": providers}
        for cname, cspec in self.client_auth["clients"].items():
            if cspec.get("data_class") is not None and cspec["data_class"] not in classes:
                return invalid(f"client_auth.clients.{cname}.data_class muss eine der Datenklassen {classes} sein")

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
        # Hysterese (0.6.8): einmal busy, bleibt der Knoten busy, solange die Auslastung ueber dieser Schwelle liegt UND fremdes
        # VRAM belegt ist (util_requires_foreign_gib) - ein Spiel mit 31-44 % Last flatterte sonst um die Eintrittsschwelle
        self.busy_exit_util = float((m.get("busy_exit") or {}).get("gpu_util_pct", _d("modes.busy_exit.gpu_util_pct")))
        # Agent >= 0.17.0 meldet Spiel und Hauptspeicher (Router 0.7.0): ein Spiel macht busy (nach game_sustain_s), danach
        # wartet free game_quiet_s; Speicherdruck (RAM- oder Commit-frei unter min_free_gib, polls Heartbeats in Folge) sperrt
        # kaltes Laden und Vorwaermen, entlaedt aber nichts
        self.busy_game = bool(be.get("game", _d("modes.busy_enter.game")))
        self.game_sustain = float(be.get("game_sustain_s", _d("modes.busy_enter.game_sustain_s")))
        # 0.8.7 / Agent 0.19.0: meldet der Agent den GPU-Bedarf des Spiels, macht es erst busy, wenn das Spiel selbst so viel
        # Grafikspeicher (eigen + geteilt) oder Engine-Auslastung braucht - oder bei Speicherdruck. Ohne Meldung wie bisher.
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
        self.unload_on_busy_interval_s = float(m.get("unload_on_busy_interval_s", _d("modes.unload_on_busy_interval_s")))   # Mindestabstand des Sicherheitsnetzes
        self.vram_settle_s = float(m.get("vram_settle_s", _d("modes.vram_settle_s")))   # Nachlauf, bis nvidia-smi einen Unload nachvollzogen hat
        self.overhead_gib = float(m.get("fit_overhead_gib", _d("modes.fit_overhead_gib")))
        # warm zuerst: ein bereits geladenes Modell aus der Tier-Liste schlaegt einen Kaltstart eines hoeheren Tiers
        self.warm_first = bool(m.get("warm_first", _d("modes.warm_first")))
        # Vorwaermen der Rang-1-Modelle, damit die Liste nach busy nicht dauerhaft beim Kleinmodell haengen bleibt
        pw = m.get("prewarm") or {}
        self.prewarm_on_free = bool(pw.get("on_free", _d("modes.prewarm.on_free")))
        self.prewarm_on_online = bool(pw.get("on_online", _d("modes.prewarm.on_online")))
        self.prewarm_free_delay = float(pw.get("free_delay_s", _d("modes.prewarm.free_delay_s")))
        self.prewarm_online_delay = float(pw.get("online_delay_s", _d("modes.prewarm.online_delay_s")))
        # Residenz: hat ein konkret angefordertes Fremdmodell das Rang-1-Modell verdraengt und wird seit residency_idle_s
        # nicht mehr gebraucht, wird Rang-1 wieder vorgewaermt. Ohne das bleibt bei "warm zuerst" die Ausweichstufe auf
        # einem ANDEREN Knoten dauerhaft bevorzugt (2026-09-10: glm verdraengte qwen, alles lief auf gpu-laptop/gemma4:12b). 0 = aus.
        self.residency_idle_s = float(pw.get("residency_idle_s", _d("modes.prewarm.residency_idle_s")))
        self.residency_check_s = float(pw.get("residency_check_s", _d("modes.prewarm.residency_check_s")))
        # Stufe 3 Scheduler: gewichtbarer Score (scheduler.score), Circuit Breaker je Knoten, Admission Control
        self.score = {k: float(_d(f"modes.score.{k}")) for k in settings_mod.SCORE_KEYS}
        for k, v in (m.get("score") or {}).items():
            if k not in self.score:
                return invalid(f"modes.score.{k}: unbekanntes Gewicht (bekannt: {', '.join(self.score)})")
            self.score[k] = float(v)
        br = m.get("breaker") or {}
        self.breaker = {"failures": int(br.get("failures", _d("modes.breaker.failures"))), "window_s": float(br.get("window_s", _d("modes.breaker.window_s"))),
                        "open_s": float(br.get("open_s", _d("modes.breaker.open_s")))}
        ad = m.get("admission") or {}
        self.admission = {"max_inflight_default": int(ad.get("max_inflight_default", _d("modes.admission.max_inflight_default"))),
                          "aging_s": float(ad.get("aging_s", _d("modes.admission.aging_s"))),
                          "max_wait_s": float(ad.get("max_wait_s", _d("modes.admission.max_wait_s"))), "max_queue": int(ad.get("max_queue", _d("modes.admission.max_queue"))),
                          # Wartebudget je Klasse (admission.max_wait_s): max_wait_s gilt fuer normal
                          "max_wait_interactive_s": float(ad.get("max_wait_interactive_s", _d("modes.admission.max_wait_interactive_s"))),
                          "max_wait_batch_s": float(ad.get("max_wait_batch_s", _d("modes.admission.max_wait_batch_s")))}
        # GPU-Schutz (design/gpu-guard.md): der Agent setzt das Power-Limit, der Router reagiert auf dessen Status -
        # Stufe 2 (gedrosselt) deckelt die Parallelitaet, Hochlast kostet Score, Probleme (unverfuegbar, abgewaehlt,
        # Spannung, Temperatur) gehen an HA. require_fresh_status: ohne frischen Status ebenfalls deckeln (kostet
        # Parallelitaet bei jedem Agent-Ausfall, darum Standard aus).
        gg = m.get("gpu_guard") or {}
        self.gpu_guard = {"enabled": bool(gg.get("enabled", _d("modes.gpu_guard.enabled"))),
                          "throttled_max_inflight": int(gg.get("throttled_max_inflight", _d("modes.gpu_guard.throttled_max_inflight"))),
                          "score_penalty": float(gg.get("score_penalty", _d("modes.gpu_guard.score_penalty"))),
                          "require_fresh_status": bool(gg.get("require_fresh_status", _d("modes.gpu_guard.require_fresh_status")))}
        # Agent-Update ueber den Router (agentupdate.py): Rollout-Schleife an/aus, Kanarienvogel-Knoten, Wartezeit, optionaler
        # oeffentlicher Betreiber-Schluessel zur Selbstpruefung des Manifests vor dem Versand.
        au = m.get("agent_update") or {}
        self.agent_update = {"enabled": bool(au.get("enabled", _d("modes.agent_update.enabled"))), "canary": au.get("canary") or "",
                             "canary_clean_h": float(au.get("canary_clean_h", _d("modes.agent_update.canary_clean_h"))), "public_key": au.get("public_key") or ""}
        # Ollama-Update ueber den Router (ollamaupdate.py, 0.3.0): Versionspruefung bei GitHub, Rollout im Nachtfenster,
        # Kanarienvogel (leer = der des Agent-Updates).
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
            # nur pruefen, wo es eine Zeitzonendatenbank gibt: die lokale Schema-Pruefung von deploy.py laeuft auch auf Windows-Python
            # ohne tzdata und wuerde sonst jede Zone ablehnen (2026-09-27)
            if zoneinfo.available_timezones() and self.ollama_update["timezone"] not in zoneinfo.available_timezones():
                return invalid(f"modes.ollama_update.timezone: unbekannte Zeitzone {self.ollama_update['timezone']!r}")
        for k in ("window_start", "window_end"):
            v = self.ollama_update[k]
            if v and not re.match(r"^\d{2}:\d{2}$", v):
                return invalid(f"modes.ollama_update.{k}: HH:MM erwartet, nicht {v!r}")

    def _parse_models(self, models):
        self.models = models
        for mname, spec in self.models.items():
            caps = (spec or {}).get("capabilities")
            if caps is not None and (not isinstance(caps, dict) or not all(isinstance(v, bool) for v in caps.values())):
                return invalid(f"models.{mname}.capabilities muss ein Mapping Faehigkeit -> true/false sein")
            if (spec or {}).get("cloud"):
                if spec["cloud"] not in self.cloud["providers"]:
                    return invalid(f"models.{mname}.cloud: Anbieter {spec['cloud']} ist unter router.cloud.providers nicht konfiguriert")
            elif (spec or {}).get("weights_gib") is None:
                return invalid(f"models.{mname}: weights_gib fehlt (lokales Modell) oder cloud: <anbieter> (Cloud-Modell)")

    def _parse_roles(self, roles):
        self.roles = {}
        for rname, r in roles.items():
            exposed = r.get("exposed_as", f"{rname}:latest")
            tiers = []
            for t in r["tiers"]:
                if t["model"] not in self.models:
                    return invalid(f"role {rname}: tier model {t['model']} fehlt im Modellkatalog")
                tier = {"model": t["model"], "num_ctx": int(t["num_ctx"]), "busy_ok": bool(t.get("busy_ok", False))}
                if (self.models[t["model"]] or {}).get("cloud"):
                    tier["cloud"] = self.models[t["model"]]["cloud"]   # Stufe 5: Cloud-Stufe
                tiers.append(tier)
            lf = r.get("latency_first")   # None = globale Einstellung warm_first
            prio = r.get("priority", "normal")   # Stufe 3: Standardklasse der Rolle (routing.priority ueberschreibt)
            if prio not in PRIORITIES:
                return invalid(f"role {rname}: priority muss eine von {PRIORITIES} sein")
            role = {"name": rname, "exposed": exposed, "tiers": tiers,
                    "latency_first": self.warm_first if lf is None else bool(lf),
                    "latency_first_override": lf, "priority": prio}
            for kind in ("canary", "shadow"):   # Stufe 6: Kandidatenmodell mit echtem Verkehr messen
                spec = r.get(kind)
                if spec:
                    if spec.get("model") not in self.models:
                        return invalid(f"role {rname}: {kind} model {spec.get('model')} fehlt im Modellkatalog")
                    pct = float(spec.get("percent", 0))
                    if not 0 <= pct <= 100:
                        return invalid(f"role {rname}: {kind}.percent muss zwischen 0 und 100 liegen")
                    role[kind] = {"model": spec["model"], "num_ctx": int(spec.get("num_ctx", tiers[0]["num_ctx"])), "percent": pct,
                                  "busy_ok": bool(spec.get("busy_ok", False))}
            self.roles[exposed] = role

    def _parse_decision(self, de):
        """decision_engine pruefen: Optionen sind Rollen (Kurzname), die Auto-Rolle darf keine echte Rolle sein, die Kette
        kennt nur bekannte Engines und jevlike braucht einen Endpunkt."""
        out = dict(de)
        if not de.get("enabled"):
            out["enabled"] = False
            return out
        role_names = {r["name"] for r in self.roles.values()}
        role = str(de.get("role") or "auto")
        if role in role_names or f"{role}:latest" in self.roles:
            return invalid(f"decision_engine.role {role!r} ist schon eine Rolle")
        options = [str(o) for o in (de.get("options") or [])]
        unknown = [o for o in options if o not in role_names]
        if len(options) < 2 or unknown:
            return invalid(f"decision_engine.options: mindestens zwei Rollen, unbekannt: {unknown}")
        default = str(de.get("default") or options[0])
        if default not in options:
            return invalid(f"decision_engine.default {default!r} steht nicht in options")
        chain = [str(e) for e in (de.get("chain") or ["rules"])]
        bad = [e for e in chain if e not in DECISION_ENGINES]
        if bad:
            return invalid(f"decision_engine.chain: unbekannte Engine {bad} (erlaubt: {DECISION_ENGINES})")
        if "jevlike" in chain and not (de.get("jevlike") or {}).get("endpoint"):
            return invalid("decision_engine.jevlike.endpoint fehlt, aber jevlike steht in der Kette")
        if "local_llm" in chain and not (de.get("local_llm") or {}).get("model"):
            return invalid("decision_engine.local_llm.model fehlt, aber local_llm steht in der Kette")
        light = [str(o) for o in (de.get("observe_light") or [])]
        if [o for o in light if o not in options]:
            return invalid(f"decision_engine.observe_light: nur Optionen der Engine ({options}), nicht {light}")
        if "embed" in chain and not (de.get("embed") or {}).get("endpoint"):
            return invalid("decision_engine.embed.endpoint fehlt, aber embed steht in der Kette")
        if "tfidf" in chain and not (de.get("tfidf") or {}).get("model_path"):
            return invalid("decision_engine.tfidf.model_path fehlt, aber tfidf steht in der Kette")
        for k, lo, hi in (("min_top_probability", 0, 1), ("min_margin", 0, 1), ("max_entropy_ratio", 0, 1)):
            v = (de.get("policy") or {}).get(k)
            if v is not None and not lo <= float(v) <= hi:
                return invalid(f"decision_engine.policy.{k} muss zwischen {lo} und {hi} liegen")
        out.update({"enabled": True, "role": role, "options": options, "default": default, "chain": chain})
        return out

    def catalog_twin(self, model):
        """Katalog-Eintrag für einen Namen: direkt oder über gleichen Digest (Alias wie `mein-assistent`)."""
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
        """Echte Belegung (ohne Fit-Zuschlag) eines geladenen Modells bei `ctx`, wenn der Katalog sie gemessen hat
        (`real: true`, seit 0.3.5), sonst None. /api/ps meldet nur einen Teil: qwen3.6:35b-a3b bei 196k 21,0 GiB,
        nvidia-smi zeigt 26,4 GiB mehr als mit leerem Ollama (gemessen 2026-09-30). Ohne diese Zahl rechnete der
        Router geladene Modelle zu klein an - der Rest fiel in die Desktop-Grundlast oder galt als fremdes VRAM."""
        m = self.catalog_twin(model)
        if m is None or m.get("cloud") or not m.get("real") or not ctx:
            return None
        return float(m["weights_gib"]) + float(m.get("kv_gib_per_1k", 0)) * ctx / 1000.0

    def max_parallel(self, model):
        """Katalog `max_parallel` (auch ueber den Digest-Zwilling eines Alias) oder None = keine Grenze je Modell."""
        m = self.catalog_twin(model)
        v = m.get("max_parallel") if m is not None else None
        return int(v) if v else None

    def need_gib(self, model, ctx, node=None):
        m = self.catalog_twin(model)
        if m is not None and m.get("cloud"):
            return 0.0
        if m is not None:
            # Gemessen per nvidia-smi (real, seit 0.3.5): das ist der ganze Bedarf. Der Fit-Zuschlag glich nur die zu
            # kleinen /api/ps-Zahlen aus; die Sicherheit liefert die Reserve im Budget (modes.vram_reserve_gib).
            extra = 0.0 if m.get("real") else self.overhead_gib
            return float(m["weights_gib"]) + float(m.get("kv_gib_per_1k", 0)) * ctx / 1000.0 + extra
        if node is not None and node.is_loaded(model):
            return node.loaded_size(model) + 0.5          # schon resident, passt per Definition
        # unbekannt und kalt: Dateigrösse aus /api/tags als Gewichte, KV unbekannt -> 20 % Aufschlag
        size = max((n.model_details.get(model, {}).get("size", 0) for n in state.NODES.values()), default=0) / GIB
        return size * 1.2 + self.overhead_gib


def cert_fingerprint(pem_path):
    """SHA-256 (hex) ueber das DER-Zertifikat einer PEM-Datei."""
    with open(pem_path, encoding="utf-8") as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    return hashlib.sha256(der).hexdigest()


def write_overrides(new):
    """roles.yaml validieren (Probe-Config) und atomar schreiben, dann live neu laden. None = geschrieben und aktiv; ein
    Problem 400 = ungueltig (nichts geschrieben), 500 = Datei nicht schreibbar (nichts geaendert)."""
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
            f.write("# Von der Router-UI geschrieben (%s). Ueberschreibt roles/models/expose_concrete_models aus config.yaml;\n"
                    "# settings = einzelne Einstellungen (Pfad -> Wert, Allowlist in settings.py), delete_models = per UI entfernte Katalogeintraege.\n"
                    % time.strftime("%Y-%m-%d %H:%M:%S"))
            yaml.safe_dump(new, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, state.CFG.overrides_path)
    except OSError as e:  # exception-ok: file system (disk full, permissions) - only the write itself can tell
        return Problem(500, f"roles.yaml not written: {type(e).__name__}")
    if failed(problem := state.CFG.reload()):   # die Probe war gueltig - hier nur, falls sich config.yaml dazwischen aenderte
        return problem
    from . import cloud, decision
    cloud.setup()
    decision.ensure()
    return None


def read_overrides():
    """roles.yaml geparst (Cache nach mtime). Aufrufer, die den Baum veraendern, muessen kopieren."""
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
