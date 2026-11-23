"""Content check before a cloud tier (router 0.13.0): a client whose data is personal may still reach the cloud for a
single request when that request carries nothing personal.

By default the data class is a declaration (cloud.py): a client without `data_class` counts as `personal`, which is
above `max_cloud_data_class`, so its requests never reach a cloud tier. A client with `privacy_check: true` opts into
this module instead: its requests keep the cloud tiers as candidates, and when a cloud tier is actually chosen (no local
node could serve), `check` decides for that one request:

1. Rules on what the user wrote (every user message): the operator's terms (names of family members, places), and the
   patterns and keyword groups of the configured locale pack (privacy_locale.py; default `ch-de`: e-mail addresses,
   phone numbers, IBAN, AHV numbers, street addresses, keyword groups for health, finances, credentials, relationships,
   law). Deterministic, no model.
2. A classifier on the same text: the router host's embedding service (decision-embed, `POST /privacy`) returns the
   probability that the text is personal. Above the threshold the request stays local.
3. Redaction of the house context: configured tools lose their whole result, and the configured redactors
   (redactors.py; default `ha_assist`: entries of configured domains, device classes and names in Home Assistant's live
   context) redact the other tool results, before anything leaves the house. Since router 0.42.0 (ADR-0125) the operator
   can choose that a tool result no redactor recognises keeps the request local (`unrecognised_tool_results:
   keep_local`); the default lets it pass as before.

Fail closed: a classifier that does not answer, answers late or answers nonsense keeps the request local, and so does a
result of a tool the operator named for redaction that cannot be redacted. A request that is not allowed gets the
configured refusal as its answer (a spoken sentence for a voice assistant) instead of an error; the journal records the
outcome and which rule or the probability, never the text.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field

from aiohttp import ClientError, ClientSession, ClientTimeout

from .outcome import invalid
from .privacy_locale import DEFAULT_LOCALE, PACKS
from .redactors import REDACTORS, GenericJsonRedactor, HaAssistRedactor

# The default pack's values under their earlier names (router 0.13.0 up to 0.42.0).
DEFAULT_REFUSAL = PACKS[DEFAULT_LOCALE].refusal
REDACTED = PACKS[DEFAULT_LOCALE].marker
KEYWORDS = PACKS[DEFAULT_LOCALE].keywords
PATTERNS = PACKS[DEFAULT_LOCALE].patterns
DEFAULT_REDACTORS = ("ha_assist",)
UNRECOGNISED_MODES = ("pass", "keep_local")


@dataclass(frozen=True)
class PrivacyConfig:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:8082"   # decision-embed on the router host (POST /privacy)
    timeout_s: float = 2.0
    threshold: float | None = None             # None = the classifier's own threshold (chosen for recall at training)
    terms: tuple = ()                          # operator terms: names, places - whole words, case-insensitive
    keywords: bool = True                      # the locale pack's keyword groups on
    redact_domains: tuple = ("person", "device_tracker", "zone", "calendar", "todo", "lock", "alarm_control_panel",
                             "camera", "image")
    redact_device_classes: tuple = ("motion", "occupancy", "presence", "door", "window", "opening", "garage_door",
                                    "lock", "tamper")
    redact_names: tuple = ()                   # entity names (substring, case-insensitive) removed from tool results
    redact_tools: tuple = ("todo_get_items",)   # tools whose whole result is removed (a list's items)
    refusal: str = DEFAULT_REFUSAL             # without the key: the locale pack's refusal
    locale: str = DEFAULT_LOCALE               # since 0.42.0: patterns, keyword groups, refusal, marker (privacy_locale.py)
    redactors: tuple = DEFAULT_REDACTORS       # since 0.42.0: tool formats redacted, in this order (redactors.py)
    generic_json: dict | None = None           # since 0.42.0: {tools: {name or "*": [dotted path]}, keys: [name]}
    unrecognised_tool_results: str = "pass"    # since 0.42.0 (ADR-0125): pass | keep_local
    terms_rx: re.Pattern | None = field(default=None, compare=False)
    chain: tuple = field(default=(), compare=False)   # the redactor objects, built by parse_privacy_config


FIELDS = tuple(k for k in PrivacyConfig.__dataclass_fields__ if k not in ("terms_rx", "chain"))
GENERIC_JSON_FIELDS = ("tools", "keys")


def _strings(value):
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def _parse_generic_json(raw):
    """router.cloud.privacy_check.generic_json -> {"tools": {name: [path]}, "keys": [name]} or a Problem."""
    where = "router.cloud.privacy_check.generic_json"
    if not isinstance(raw, dict) or set(raw) - set(GENERIC_JSON_FIELDS):
        return invalid(f"{where} must be a mapping with the keys {', '.join(GENERIC_JSON_FIELDS)}")
    tools, keys = raw.get("tools"), raw.get("keys", [])
    if not isinstance(tools, dict) or not tools:
        return invalid(f"{where}.tools must map tool names (or \"*\" for every JSON result) to lists of paths")
    for name, paths in tools.items():
        if not isinstance(name, str) or not name.strip() or not _strings(paths):
            return invalid(f"{where}.tools.{name} must be a list of dotted paths (an empty list redacts only keys)")
        if any("" in p.strip().split(".") for p in paths):
            return invalid(f"{where}.tools.{name}: a path has an empty segment")
    if not _strings(keys):
        return invalid(f"{where}.keys must be a list of key names")
    return {"tools": {n.strip(): [p.strip() for p in ps] for n, ps in tools.items()}, "keys": [k.strip() for k in keys]}


def _build_chain(cfg_values):
    """The redactor objects for the configured names, in order."""
    built = []
    for name in cfg_values["redactors"]:
        if name == "ha_assist":
            built.append(HaAssistRedactor(cfg_values["redact_domains"], cfg_values["redact_device_classes"],
                                          cfg_values["redact_names"]))
        else:
            gj = cfg_values["generic_json"]
            built.append(GenericJsonRedactor(tuple((n, tuple(ps)) for n, ps in gj["tools"].items()), tuple(gj["keys"])))
    return tuple(built)


def parse_privacy_config(raw):
    """router.cloud.privacy_check -> PrivacyConfig or a Problem."""
    if raw is None:
        raw = {}
    d = PrivacyConfig()
    if not isinstance(raw, dict) or set(raw) - set(FIELDS):
        return invalid(f"unknown key in router.cloud.privacy_check (allowed: {', '.join(FIELDS)})")
    v = {k: raw.get(k, getattr(d, k)) for k in FIELDS}
    for k in ("enabled", "keywords"):
        if type(v[k]) is not bool:
            return invalid(f"router.cloud.privacy_check.{k} must be boolean")
    if not isinstance(v["endpoint"], str) or not v["endpoint"].startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return invalid("router.cloud.privacy_check.endpoint must be a loopback URL (the check must not leave the host)")
    t = v["timeout_s"]
    if type(t) not in (int, float) or not math.isfinite(t) or not 0.1 <= t <= 10:
        return invalid("router.cloud.privacy_check.timeout_s must be between 0.1 and 10")
    th = v["threshold"]
    if th is not None and (type(th) not in (int, float) or not 0 < th < 1):
        return invalid("router.cloud.privacy_check.threshold must be between 0 and 1 (or null)")
    for k in ("terms", "redact_domains", "redact_device_classes", "redact_names", "redact_tools"):
        if not isinstance(v[k], (list, tuple)) or any(not isinstance(x, str) or not x.strip() for x in v[k]):
            return invalid(f"router.cloud.privacy_check.{k} must be a list of non-empty strings")
        v[k] = tuple(x.strip() for x in v[k])
    if not isinstance(v["locale"], str) or v["locale"] not in PACKS:
        return invalid(f"router.cloud.privacy_check.locale must be one of {', '.join(PACKS)}")
    if "refusal" not in raw:
        v["refusal"] = PACKS[v["locale"]].refusal
    if not isinstance(v["refusal"], str) or not v["refusal"].strip():
        return invalid("router.cloud.privacy_check.refusal must be a non-empty sentence")
    reds = v["redactors"]
    # at least one redactor: an empty list would switch the house-context redaction off without saying so
    if not isinstance(reds, (list, tuple)) or not reds or any(r not in REDACTORS for r in reds) or len(set(reds)) != len(reds):
        return invalid(f"router.cloud.privacy_check.redactors must list at least one of {', '.join(REDACTORS)}, each once")
    v["redactors"] = tuple(reds)
    # a generic_json section without the redactor would redact nothing although it looks configured, and vice versa
    if ("generic_json" in v["redactors"]) != (v["generic_json"] is not None):
        return invalid("router.cloud.privacy_check.generic_json and the redactor 'generic_json' go together: "
                       "configure both or neither")
    if v["generic_json"] is not None:
        gj = _parse_generic_json(v["generic_json"])
        if not isinstance(gj, dict):
            return gj
        v["generic_json"] = gj
    if v["unrecognised_tool_results"] not in UNRECOGNISED_MODES:
        return invalid(f"router.cloud.privacy_check.unrecognised_tool_results must be one of {', '.join(UNRECOGNISED_MODES)}")
    rx = re.compile(r"(?i)(?<![\wäöü])(?:" + "|".join(re.escape(x) for x in v["terms"]) + r")(?![\wäöü])") if v["terms"] else None
    return PrivacyConfig(**{**v, "timeout_s": float(t), "terms_rx": rx, "chain": _build_chain(v)})


@dataclass
class Verdict:
    allowed: bool
    result: str            # passed | personal | unavailable | unredacted
    by: str = ""           # rules:<group> | classifier | redaction:<redactor> | redaction:unrecognised
    probability: float | None = None
    redacted: int = 0
    body: dict | None = None   # redacted copy to send (allowed only)

    def journal(self):
        return {"result": self.result, "by": self.by or None, "p": None if self.probability is None else round(self.probability, 3),
                "redacted": self.redacted}


def user_texts(body):
    """What the user wrote: the text of every user message (chat) or the prompt (generate). A data message the router
    inserted from memory (memory.packed) also yields its facts as readable text: the packed form escapes non-ASCII
    characters, so a term like "Müller" in a fact would otherwise never match (TM-05)."""
    from .memory import unpacked_texts   # late: memory imports modules that import this one
    out = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
            out += unpacked_texts(c)
        elif isinstance(c, list):
            out += [p.get("text") for p in c if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if isinstance(body.get("prompt"), str):
        out.append(body["prompt"])
    return [t for t in out if t.strip()]


def rule_hit(texts, cfg):
    """The first rule that matches what the user wrote, or None."""
    pack = PACKS[cfg.locale]
    for t in texts:
        if cfg.terms_rx is not None and cfg.terms_rx.search(t):
            return "terms"
        for name, rx in pack.patterns.items():
            if rx.search(t):
                return name
        if cfg.keywords:
            for name, rx in pack.keyword_rx.items():
                if rx.search(t):
                    return name
    return None


def _chain(cfg):
    """The configured redactors. A PrivacyConfig built directly instead of parsed has no chain; with the default
    redactor it gets ha_assist from its own fields, anything else is a programming error."""
    if cfg.chain:
        return cfg.chain
    if cfg.redactors != DEFAULT_REDACTORS or cfg.generic_json is not None:
        raise ValueError("PrivacyConfig with redactors must come from parse_privacy_config")  # exception-ok: programming error
    return (HaAssistRedactor(cfg.redact_domains, cfg.redact_device_classes, cfg.redact_names),)


@dataclass
class BodyRedaction:
    body: dict
    removed: int = 0
    keep_local: str = ""   # "" = may leave; else the reason (redaction:<redactor> | redaction:unrecognised)


def redact_request(body, cfg):
    """Copy of the request with the house context redacted, the number of entries removed and whether a tool result
    keeps the request local (a named tool whose result cannot be redacted, or an unrecognised result in keep_local
    mode)."""
    out = copy.deepcopy(body)
    removed, keep_local = 0, ""
    marker = PACKS[cfg.locale].marker
    chain = _chain(cfg)
    for m in out.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        if m.get("tool_name") in cfg.redact_tools or m.get("name") in cfg.redact_tools:
            m["content"] = marker
            removed += 1
            continue
        names = tuple(n for n in (m.get("tool_name"), m.get("name")) if isinstance(n, str))
        content, recognised = m.get("content"), False
        for redactor in chain:
            r = redactor.redact(names, content)
            content, removed, recognised = r.content, removed + r.removed, recognised or r.recognised
            if r.unredactable and not keep_local:
                keep_local = f"redaction:{redactor.name}"
        m["content"] = content
        if not recognised and not keep_local and cfg.unrecognised_tool_results == "keep_local":
            keep_local = "redaction:unrecognised"
    return BodyRedaction(out, removed, keep_local)


def redact_body(body, cfg):
    """Copy of the request with the house context redacted; (copy, entries removed)."""
    r = redact_request(body, cfg)
    return r.body, r.removed


async def classify(texts, cfg):
    """Probability that the text is personal, from the embedding service, and the threshold to apply.
    (None, None) = no usable answer (the caller keeps the request local)."""
    try:
        async with ClientSession(timeout=ClientTimeout(total=cfg.timeout_s)) as s:
            async with s.post(cfg.endpoint.rstrip("/") + "/privacy", json={"text": "\n".join(texts)}) as r:
                if r.status != 200:
                    return None, None
                d = await r.json(content_type=None)
    except (ClientError, TimeoutError, ValueError, OSError):  # exception-ok: network boundary to the local service
        return None, None
    p, th = d.get("personal") if isinstance(d, dict) else None, d.get("threshold") if isinstance(d, dict) else None
    if type(p) not in (int, float) or not 0 <= p <= 1:
        return None, None
    th = cfg.threshold if cfg.threshold is not None else th
    if type(th) not in (int, float) or not 0 < th < 1:
        return None, None
    return float(p), float(th)


async def check(body, cfg):
    """Decide whether this request may go to a cloud tier; on yes, the verdict carries the redacted copy to send."""
    texts = user_texts(body)
    hit = rule_hit(texts, cfg)
    if hit:
        return Verdict(False, "personal", by=f"rules:{hit}")
    if texts:
        p, th = await classify(texts, cfg)
        if p is None:
            return Verdict(False, "unavailable", by="classifier")
        if p >= th:
            return Verdict(False, "personal", by="classifier", probability=p)
    else:
        p = None
    red = redact_request(body, cfg)
    if red.keep_local:
        # a tool result that must not leave unredacted: stay local like any other refusal, nothing is sent
        return Verdict(False, "unredacted", by=red.keep_local, probability=p, redacted=red.removed)
    return Verdict(True, "passed", by="classifier" if texts else "", probability=p, redacted=red.removed, body=red.body)
