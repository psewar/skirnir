"""config.yaml + roles.yaml (UI-Overrides) einlesen, validieren, live neu laden."""

import difflib
import hashlib
import os
import ssl
import time
import yaml

from . import state
from . import settings as settings_mod
from .common import GIB

PRIORITIES = ("interactive", "normal", "batch")   # Stufe 3: Prioritaetsklassen (siehe admission.py)

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
    except FileNotFoundError:
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
SCHEMA = {
    "router": {
        "listen": None, "control_listen": None, "public_url": None, "control_tls": {"cert": None, "key": None},
        "ollama_poll_s": None, "heartbeat_interval_s": None, "heartbeat_stale_s": None, "offline_after_misses": None,
        "agent_missing_problem_s": None, "expose_concrete_models": None, "request_timeout_s": None,
        "toolcall_rescue": None,
        "limits": {"max_images": None, "max_tools": None, "max_messages": None}, "session_affinity_ttl_s": None,
        "idempotency_ttl_s": None, "control_auth": {"users": None}, "heartbeat_token": None, "api_tls": None,
        "client_auth": {"mode": None, "audit_log": None, "locked": None,
                        "clients": {"*": {"token_sha256": None, "ip": None, "roles": None, "models": None, "requests_per_minute": None, "max_priority": None,
                                          "cloud": None, "data_class": None, "note": None, "token_rotated": None, "created": None}}},
        "cloud": {"enabled": None, "data_classes": None, "default_data_class": None, "max_cloud_data_class": None, "credential_scan": None,
                  "egress_allow": None,
                  "providers": {"*": {"kind": None, "base_url": None, "api_key_env": None, "secrets_file": None, "enabled": None, "budget_month_chf": None,
                                      "warn_at_percent": None, "region": None, "timeout_s": None, "max_inflight": None, "max_data_class": None}}},
        "openai": {"enabled": None, "default_think": None},
        "wol": {"wait_up_s": None, "retry_interval_s": None, "cooldown_s": None, "broadcast_addresses": None},
    },
    "mqtt": {"host": None, "port": None, "tls": None, "username": None, "password": None, "password_env": None, "password_file": None,
             "base_topic": None, "discovery_prefix": None, "interval_s": None},
    "modes": {
        "busy_enter": {"gpu_util_pct": None, "sustain_s": None, "util_requires_foreign_gib": None, "or_foreign_vram_gib": None, "foreign_sustain_s": None},
        "busy_exit": {"below_for_s": None}, "vram_reserve_gib": {"free": None, "busy": None}, "keep_alive": {"free": None, "busy": None},
        "unload_on_busy": None, "unload_on_busy_interval_s": None, "vram_settle_s": None, "fit_overhead_gib": None, "warm_first": None,
        "prewarm": {"on_free": None, "free_delay_s": None, "on_online": None, "online_delay_s": None, "residency_idle_s": None, "residency_check_s": None},
        "score": None, "breaker": {"failures": None, "window_s": None, "open_s": None},
        "admission": {"max_inflight_default": None, "aging_s": None, "max_wait_s": None, "max_queue": None},
        "gpu_guard": {"enabled": None, "throttled_max_inflight": None, "score_penalty": None, "require_fresh_status": None},
        "agent_update": {"enabled": None, "canary": None, "canary_clean_h": None, "public_key": None},
    },
    "models": {"*": {"weights_gib": None, "kv_gib_per_1k": None, "capabilities": None, "source": None, "note": None, "measured_at": None,
                     "measured_on": None, "vram_gib_8k": None, "vram_gib_32k": None, "partial_offload": None,
                     "cloud": None, "provider_model": None, "price_chf_per_m": None, "context_tokens": None, "reasoning": None}},
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
                        "capture": {"enabled": None, "path": None, "clients": None, "anonymize": None, "max_context_chars": None}},
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


class Config:
    """config.yaml (kommentiert, per deploy) + roles.yaml (von der UI geschrieben, überschreibt roles/models/expose)."""

    def __init__(self, path, use_overrides=True):
        self.path = path
        self.overrides_path = os.path.join(os.path.dirname(os.path.abspath(path)), "roles.yaml")
        self.use_overrides = use_overrides
        self.reload()

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
                c.setdefault("models", {}).update(v)
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
            settings_mod.apply(c, overrides["settings"])
        return c

    def base(self):
        """config.yaml pur (ohne roles.yaml) - fuer die Anzeige der Basiswerte in der UI. Nur lesen, nicht veraendern (Cache)."""
        return load_yaml(self.path)

    def reload(self, overrides=None):
        self.parse(self.merged(overrides))

    def parse(self, c):
        errs = validate_schema(c)
        if errs:
            raise ValueError("Konfiguration: " + "; ".join(errs))
        for sect in ("router", "modes", "roles"):
            if not isinstance(c.get(sect), dict):
                raise ValueError(f"Konfiguration: Abschnitt {sect} fehlt")
        self.raw = c
        r = c["router"]
        self.listen = r.get("listen", "0.0.0.0:11434")
        self.control_listen = r.get("control_listen", "0.0.0.0:11435")
        self.poll_s = float(r.get("ollama_poll_s", 5))
        self.offline_after = int(r.get("offline_after_misses", 3))
        self.hb_interval_s = float(r.get("heartbeat_interval_s", 3))   # Takt, in dem Agenten GPU-Werte melden (wird provisioniert)
        self.hb_stale_s = float(r.get("heartbeat_stale_s", 10))
        if self.hb_stale_s < 2 * self.hb_interval_s:   # sonst gilt ein Knoten zwischen zwei Meldungen als "GPU unbekannt"
            self.hb_stale_s = 2 * self.hb_interval_s + 1
        self.agent_missing_s = float(r.get("agent_missing_problem_s", 300))   # Agent war da und schweigt so lange -> HA-Problem
        self.expose_concrete = bool(r.get("expose_concrete_models", False))
        self.request_timeout_s = float(r.get("request_timeout_s", 600))
        self.heartbeat_token = r.get("heartbeat_token") or ""
        self.api_tls = bool(r.get("api_tls", False))   # 11434 ebenfalls TLS (Zertifikat aus control_tls)
        # Im Text gestrandete Tool-Calls zurueckholen (siehe toolcall_rescue). Standard an:
        # die Stufe greift nur, wenn Ollama selbst nichts geparst hat, und laesst sonst alles unberuehrt.
        self.toolcall_rescue = bool(r.get("toolcall_rescue", True))
        # OpenAI-kompatible Schnittstelle /v1 (Node-RED-MCP, andere OpenAI-Clients). Der Router uebersetzt selbst nach
        # /api/chat, weil Ollamas eigenes /v1 kein num_ctx/keep_alive kennt (Modell wuerde mit Default-Kontext neu laden).
        oa = r.get("openai") or {}
        self.openai_enabled = bool(oa.get("enabled", True))
        # think, wenn der Client nichts sagt: false = Antwort ohne unsichtbares Nachdenken (wie HAs AI-Tasks);
        # true = Modell-Default; null = Feld nicht setzen (Ollama entscheidet)
        self.openai_default_think = oa.get("default_think", False)
        # Basic Auth für UI + /admin/*: {user: "pbkdf2:<iter>:<salt_hex>:<hash_hex>"}; leer = offen (nur Tests)
        self.control_users = dict((r.get("control_auth") or {}).get("users") or {})
        # Client-Authentifizierung auf dem API-Port 11434 (Stufe 2, design/roadmap.md). mode: observe = alles bedienen,
        # Unbekannte zaehlen und ins Audit-Log; enforce = 401 ohne gueltige Identitaet. clients: {name: {token_sha256,
        # ip: [..], roles: [..]|["*"], models: bool, requests_per_minute}}. Kein clients-Block = Port offen wie bisher.
        ca = r.get("client_auth") or {}
        if ca.get("mode", "observe") not in ("observe", "enforce"):
            raise ValueError(f"client_auth.mode muss observe oder enforce sein, nicht {ca.get('mode')!r}")
        clients = {}
        for name, spec in (ca.get("clients") or {}).items():
            spec = dict(spec or {})
            th = (spec.get("token_sha256") or "").strip().lower()
            if th and (len(th) != 64 or any(ch not in "0123456789abcdef" for ch in th)):
                raise ValueError(f"client_auth.clients.{name}.token_sha256 muss 64 Hex-Zeichen sein")
            spec["token_sha256"] = th
            spec["ip"] = [str(x) for x in (spec.get("ip") or [])]
            spec["roles"] = [str(x) for x in (spec.get("roles") or ["*"])]
            spec["models"] = bool(spec.get("models", True))
            if spec.get("requests_per_minute") is not None:
                spec["requests_per_minute"] = int(spec["requests_per_minute"])
            if spec.get("max_priority") is not None and spec["max_priority"] not in PRIORITIES:   # Stufe 3: Kappung der Klasse
                raise ValueError(f"client_auth.clients.{name}.max_priority muss eine von {PRIORITIES} sein")
            clients[str(name)] = spec
        # locked (Audit 2026-09-16): true = der Modus ist NUR ueber config.yaml + Deploy aenderbar; UI-Einstellung und
        # POST /admin/client_auth werden abgelehnt (403/400 + Audit-Eintrag). Ein kompromittiertes UI-Konto kann die
        # Client-Auth dann nicht mehr per Klick auf observe stellen.
        self.client_auth = {"mode": ca.get("mode", "observe"), "clients": clients, "locked": bool(ca.get("locked", False)),
                            "audit_log": ca.get("audit_log", "/var/log/ollama-router/audit.jsonl")}
        # TLS für den Control-Port: {cert: <fullchain.pem>, key: <key.pem>}; fehlt der Block -> Klartext (nur Tests)
        tls = r.get("control_tls") or {}
        self.control_tls = (tls.get("cert"), tls.get("key")) if tls.get("cert") and tls.get("key") else None
        self.public_url = r.get("public_url", "")
        # Stufe 1: Request-Groessenlimits (413 vor dem Backend) und Verfall der Session-Affinitaet
        lim = r.get("limits") or {}
        self.limits = {"max_images": int(lim.get("max_images", 16)), "max_tools": int(lim.get("max_tools", 128)),
                       "max_messages": int(lim.get("max_messages", 1000))}
        self.session_ttl_s = float(r.get("session_affinity_ttl_s", 1800))
        self.idempotency_ttl_s = float(r.get("idempotency_ttl_s", 600))   # Stufe 6: Wiederholungen mit Idempotency-Key
        # Stufe 5: Cloud-Anbieter als Stufen in Rollen (cloud.py). Kein Opt-in pro Client: die Rolle erlaubt, der Client kann
        # sich per routing.execution: local oder clients.<c>.cloud: false ausnehmen.
        from urllib.parse import urlparse
        cl = r.get("cloud") or {}
        classes = [str(x) for x in (cl.get("data_classes") or ["public", "internal", "personal", "secret"])]
        for key, default in (("default_data_class", "personal"), ("max_cloud_data_class", "internal")):
            if cl.get(key, default) not in classes:
                raise ValueError(f"router.cloud.{key} muss eine der Datenklassen {classes} sein")
        if cl.get("credential_scan", "block") not in ("block", "off"):
            raise ValueError("router.cloud.credential_scan muss block oder off sein")
        allow = {"api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com"} | {str(h) for h in (cl.get("egress_allow") or [])}
        providers = {}
        for pname, spec in (cl.get("providers") or {}).items():
            spec = dict(spec or {})
            if spec.get("kind") not in ("openai", "anthropic"):
                raise ValueError(f"router.cloud.providers.{pname}.kind muss openai oder anthropic sein")
            host = urlparse(str(spec.get("base_url") or "")).hostname
            if not host or host not in allow:
                raise ValueError(f"router.cloud.providers.{pname}.base_url: Host {host!r} nicht in der Egress-Allowlist {sorted(allow)}")
            if not spec.get("api_key_env"):
                raise ValueError(f"router.cloud.providers.{pname}.api_key_env fehlt")
            if spec.get("max_data_class") is not None and spec["max_data_class"] not in classes:
                raise ValueError(f"router.cloud.providers.{pname}.max_data_class muss eine der Datenklassen {classes} sein")
            spec["enabled"] = bool(spec.get("enabled", True))
            providers[str(pname)] = spec
        self.cloud = {"enabled": bool(cl.get("enabled", False)), "data_classes": classes,
                      "default_data_class": cl.get("default_data_class", "personal"), "max_cloud_data_class": cl.get("max_cloud_data_class", "internal"),
                      "credential_scan": cl.get("credential_scan", "block"), "egress_allow": sorted(allow), "providers": providers}
        for cname, cspec in self.client_auth["clients"].items():
            if cspec.get("data_class") is not None and cspec["data_class"] not in classes:
                raise ValueError(f"client_auth.clients.{cname}.data_class muss eine der Datenklassen {classes} sein")
        # Home Assistant per MQTT-Discovery (optional): host, port, username, password_env, base_topic, interval_s
        self.mqtt = c.get("mqtt") or None
        w = r.get("wol", {})
        self.wol_wait_s = float(w.get("wait_up_s", 90))
        self.wol_retry_s = float(w.get("retry_interval_s", 20))
        self.wol_cooldown_s = float(w.get("cooldown_s", 300))
        self.wol_broadcasts = list(w.get("broadcast_addresses", ["255.255.255.255"]))

        m = c["modes"]
        be = m["busy_enter"]
        self.busy_util = float(be.get("gpu_util_pct", 40))
        self.busy_sustain = float(be.get("sustain_s", 10))
        self.busy_foreign_gib = float(be.get("or_foreign_vram_gib", 2.0))
        self.foreign_sustain = float(be.get("foreign_sustain_s", 15))
        self.util_requires_foreign_gib = float(be.get("util_requires_foreign_gib", 1.0))
        self.busy_exit_s = float(m.get("busy_exit", {}).get("below_for_s", 30))
        self.reserve = {k: float(v) for k, v in m.get("vram_reserve_gib", {"free": 1.0, "busy": 2.0}).items()}
        self.keep_alive = m.get("keep_alive", {"free": -1, "busy": "5m"})
        self.unload_on_busy = bool(m.get("unload_on_busy", True))
        self.unload_on_busy_interval_s = float(m.get("unload_on_busy_interval_s", 30))   # Mindestabstand des Sicherheitsnetzes
        self.vram_settle_s = float(m.get("vram_settle_s", 8))   # Nachlauf, bis nvidia-smi einen Unload nachvollzogen hat
        self.overhead_gib = float(m.get("fit_overhead_gib", 0.8))
        # warm zuerst: ein bereits geladenes Modell aus der Tier-Liste schlaegt einen Kaltstart eines hoeheren Tiers
        self.warm_first = bool(m.get("warm_first", True))
        # Vorwaermen der Rang-1-Modelle, damit die Liste nach busy nicht dauerhaft beim Kleinmodell haengen bleibt
        pw = m.get("prewarm") or {}
        self.prewarm_on_free = bool(pw.get("on_free", True))
        self.prewarm_on_online = bool(pw.get("on_online", True))
        self.prewarm_free_delay = float(pw.get("free_delay_s", 20))
        self.prewarm_online_delay = float(pw.get("online_delay_s", 0))
        # Residenz: hat ein konkret angefordertes Fremdmodell das Rang-1-Modell verdraengt und wird seit residency_idle_s
        # nicht mehr gebraucht, wird Rang-1 wieder vorgewaermt. Ohne das bleibt bei "warm zuerst" die Ausweichstufe auf
        # einem ANDEREN Knoten dauerhaft bevorzugt (2026-09-10: glm verdraengte qwen, alles lief auf gpu-laptop/gemma4:12b). 0 = aus.
        self.residency_idle_s = float(pw.get("residency_idle_s", 300))
        self.residency_check_s = float(pw.get("residency_check_s", 60))
        # Stufe 3 Scheduler: gewichtbarer Score (scheduler.score), Circuit Breaker je Knoten, Admission Control
        self.score = {"warm": 100.0, "inflight": 10.0, "saturated": 50.0, "vram_free": 2.0, "weight": 1.0, "speed": 2.0,
                      "errors": 20.0, "half_open": 5.0}
        for k, v in (m.get("score") or {}).items():
            if k not in self.score:
                raise ValueError(f"modes.score.{k}: unbekanntes Gewicht (bekannt: {', '.join(self.score)})")
            self.score[k] = float(v)
        br = m.get("breaker") or {}
        self.breaker = {"failures": int(br.get("failures", 3)), "window_s": float(br.get("window_s", 60)),
                        "open_s": float(br.get("open_s", 30))}
        ad = m.get("admission") or {}
        self.admission = {"max_inflight_default": int(ad.get("max_inflight_default", 2)), "aging_s": float(ad.get("aging_s", 30)),
                          "max_wait_s": float(ad.get("max_wait_s", 120)), "max_queue": int(ad.get("max_queue", 64))}
        # GPU-Schutz (design/gpu-guard.md): der Agent setzt das Power-Limit, der Router reagiert auf dessen Status -
        # Stufe 2 (gedrosselt) deckelt die Parallelitaet, Hochlast kostet Score, Probleme (unverfuegbar, abgewaehlt,
        # Spannung, Temperatur) gehen an HA. require_fresh_status: ohne frischen Status ebenfalls deckeln (kostet
        # Parallelitaet bei jedem Agent-Ausfall, darum Standard aus).
        gg = m.get("gpu_guard") or {}
        # Agent-Update ueber den Router (agentupdate.py): Rollout-Schleife an/aus, Kanarienvogel-Knoten, Wartezeit, optionaler
        # oeffentlicher Betreiber-Schluessel zur Selbstpruefung des Manifests vor dem Versand.
        au = m.get("agent_update") or {}
        self.agent_update = {"enabled": bool(au.get("enabled", True)), "canary": au.get("canary") or "",
                             "canary_clean_h": float(au.get("canary_clean_h", 24)), "public_key": au.get("public_key") or ""}
        self.gpu_guard = {"enabled": bool(gg.get("enabled", True)), "throttled_max_inflight": int(gg.get("throttled_max_inflight", 1)),
                          "score_penalty": float(gg.get("score_penalty", 40)), "require_fresh_status": bool(gg.get("require_fresh_status", False))}

        self.models = c.get("models", {})
        for mname, spec in self.models.items():
            caps = (spec or {}).get("capabilities")
            if caps is not None and (not isinstance(caps, dict) or not all(isinstance(v, bool) for v in caps.values())):
                raise ValueError(f"models.{mname}.capabilities muss ein Mapping Faehigkeit -> true/false sein")
            if (spec or {}).get("cloud"):
                if spec["cloud"] not in self.cloud["providers"]:
                    raise ValueError(f"models.{mname}.cloud: Anbieter {spec['cloud']} ist unter router.cloud.providers nicht konfiguriert")
            elif (spec or {}).get("weights_gib") is None:
                raise ValueError(f"models.{mname}: weights_gib fehlt (lokales Modell) oder cloud: <anbieter> (Cloud-Modell)")
        self.nodes = c["nodes"]
        self.roles = {}
        for rname, r in c["roles"].items():
            exposed = r.get("exposed_as", f"{rname}:latest")
            tiers = []
            for t in r["tiers"]:
                if t["model"] not in self.models:
                    raise ValueError(f"role {rname}: tier model {t['model']} fehlt im Modellkatalog")
                tier = {"model": t["model"], "num_ctx": int(t["num_ctx"]), "busy_ok": bool(t.get("busy_ok", False))}
                if (self.models[t["model"]] or {}).get("cloud"):
                    tier["cloud"] = self.models[t["model"]]["cloud"]   # Stufe 5: Cloud-Stufe
                tiers.append(tier)
            lf = r.get("latency_first")   # None = globale Einstellung warm_first
            prio = r.get("priority", "normal")   # Stufe 3: Standardklasse der Rolle (routing.priority ueberschreibt)
            if prio not in PRIORITIES:
                raise ValueError(f"role {rname}: priority muss eine von {PRIORITIES} sein")
            role = {"name": rname, "exposed": exposed, "tiers": tiers,
                    "latency_first": self.warm_first if lf is None else bool(lf),
                    "latency_first_override": lf, "priority": prio}
            for kind in ("canary", "shadow"):   # Stufe 6: Kandidatenmodell mit echtem Verkehr messen
                spec = r.get(kind)
                if spec:
                    if spec.get("model") not in self.models:
                        raise ValueError(f"role {rname}: {kind} model {spec.get('model')} fehlt im Modellkatalog")
                    pct = float(spec.get("percent", 0))
                    if not 0 <= pct <= 100:
                        raise ValueError(f"role {rname}: {kind}.percent muss zwischen 0 und 100 liegen")
                    role[kind] = {"model": spec["model"], "num_ctx": int(spec.get("num_ctx", tiers[0]["num_ctx"])), "percent": pct,
                                  "busy_ok": bool(spec.get("busy_ok", False))}
            self.roles[exposed] = role
        self.busy_ok_models = {t["model"] for r in self.roles.values() for t in r["tiers"] if t["busy_ok"]}
        self.decision = self._parse_decision(c.get("decision_engine") or {})

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
            raise ValueError(f"decision_engine.role {role!r} ist schon eine Rolle")
        options = [str(o) for o in (de.get("options") or [])]
        unknown = [o for o in options if o not in role_names]
        if len(options) < 2 or unknown:
            raise ValueError(f"decision_engine.options: mindestens zwei Rollen, unbekannt: {unknown}")
        default = str(de.get("default") or options[0])
        if default not in options:
            raise ValueError(f"decision_engine.default {default!r} steht nicht in options")
        chain = [str(e) for e in (de.get("chain") or ["rules"])]
        bad = [e for e in chain if e not in DECISION_ENGINES]
        if bad:
            raise ValueError(f"decision_engine.chain: unbekannte Engine {bad} (erlaubt: {DECISION_ENGINES})")
        if "jevlike" in chain and not (de.get("jevlike") or {}).get("endpoint"):
            raise ValueError("decision_engine.jevlike.endpoint fehlt, aber jevlike steht in der Kette")
        if "local_llm" in chain and not (de.get("local_llm") or {}).get("model"):
            raise ValueError("decision_engine.local_llm.model fehlt, aber local_llm steht in der Kette")
        if "embed" in chain and not (de.get("embed") or {}).get("endpoint"):
            raise ValueError("decision_engine.embed.endpoint fehlt, aber embed steht in der Kette")
        if "tfidf" in chain and not (de.get("tfidf") or {}).get("model_path"):
            raise ValueError("decision_engine.tfidf.model_path fehlt, aber tfidf steht in der Kette")
        for k, lo, hi in (("min_top_probability", 0, 1), ("min_margin", 0, 1), ("max_entropy_ratio", 0, 1)):
            v = (de.get("policy") or {}).get(k)
            if v is not None and not lo <= float(v) <= hi:
                raise ValueError(f"decision_engine.policy.{k} muss zwischen {lo} und {hi} liegen")
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

    def need_gib(self, model, ctx, node=None):
        m = self.catalog_twin(model)
        if m is not None and m.get("cloud"):
            return 0.0
        if m is not None:
            return float(m["weights_gib"]) + float(m.get("kv_gib_per_1k", 0)) * ctx / 1000.0 + self.overhead_gib
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
    """roles.yaml validieren (Probe-Config) und atomar schreiben, dann live neu laden."""
    probe = Config.__new__(Config)
    probe.path = state.CFG.path
    probe.overrides_path = state.CFG.overrides_path
    probe.use_overrides = True
    probe.parse(probe.merged(new))          # wirft bei Fehlern
    tmp = state.CFG.overrides_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Von der Router-UI geschrieben (%s). Ueberschreibt roles/models/expose_concrete_models aus config.yaml;\n"
                "# settings = einzelne Einstellungen (Pfad -> Wert, Allowlist in settings.py), delete_models = per UI entfernte Katalogeintraege.\n"
                % time.strftime("%Y-%m-%d %H:%M:%S"))
        yaml.safe_dump(new, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, state.CFG.overrides_path)
    state.CFG.reload()
    from . import cloud
    cloud.setup()


def read_overrides():
    """roles.yaml geparst (Cache nach mtime). Aufrufer, die den Baum veraendern, muessen kopieren."""
    import copy
    return copy.deepcopy(load_yaml(state.CFG.overrides_path, {}) or {})


def settings_view():
    ov = read_overrides()
    return settings_mod.view(state.CFG, state.CFG.base(), ov.get("settings") or {})


def roles_as_config():
    return {r["name"]: {"exposed_as": r["exposed"], "latency_first": r["latency_first_override"],
                        "latency_first_effective": r["latency_first"], "priority": r["priority"],
                        "canary": r.get("canary"), "shadow": r.get("shadow"),
                        "tiers": [dict(t) for t in r["tiers"]]} for r in state.CFG.roles.values()}
