"""Content check before a cloud tier (router 0.13.0): a client whose data is personal may still reach the cloud for a
single request when that request carries nothing personal.

By default the data class is a declaration (cloud.py): a client without `data_class` counts as `personal`, which is
above `max_cloud_data_class`, so its requests never reach a cloud tier. A client with `privacy_check: true` opts into
this module instead: its requests keep the cloud tiers as candidates, and when a cloud tier is actually chosen (no local
node could serve), `check` decides for that one request:

1. Rules on what the user wrote (every user message): the operator's terms (names of family members, places), e-mail
   addresses, phone numbers, IBAN, AHV numbers, street addresses, and built-in keyword groups (health, finances,
   credentials, relationships, law). Deterministic, no model.
2. A classifier on the same text: the router host's embedding service (decision-embed, `POST /privacy`) returns the
   probability that the text is personal. Above the threshold the request stays local.
3. Redaction of the house context: tool results (Home Assistant's live context) lose the entries of configured domains,
   device classes and names, and configured tools lose their whole result, before anything leaves the house.

Fail closed: a classifier that does not answer, answers late or answers nonsense keeps the request local. A request
that is not allowed gets the configured refusal as its answer (a spoken sentence for a voice assistant) instead of an
error; the journal records the outcome and which rule or the probability, never the text.
"""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field

from aiohttp import ClientError, ClientSession, ClientTimeout

from .outcome import invalid

DEFAULT_REFUSAL = "Das beantworte ich nur lokal, und der lokale Rechner ist gerade nicht erreichbar."
REDACTED = "[fuer die Cloud entfernt]"

# Built-in keyword groups: word starts (German and English), case-insensitive. A hit keeps the request local - false
# positives cost a cloud answer, false negatives cost privacy, so the lists lean wide.
KEYWORDS = {
    "health": r"arzt|ärzt|krank|diagnos|medikament|tablette|therapie|therapeut|psycholog|psychiat|depress|schwanger|"
              r"operation|blutdruck|blutzucker|symptom|schmerz|kopfweh|schwindl|übelkeit|erbrech|fieber|husten|spital|krankenhaus|klinik|impf|allergi|verletz|"
              r"doctor|illness|medication|pregnan|therapy|diagnos",
    "finances": r"kontostand|bankkonto|konto\b|lohn\b|löhne|gehalt|schulden|kredit|steuererkl|steuern\b|einkommen|vermögen|"
                r"hypothek|versicherungsnummer|salary|debt|bank account",
    "credentials": r"passw|kennwort|pin\b|pin-|zugangsdaten|login|benutzername|password|credential",
    "relationships": r"beziehung|scheidung|trennung|liebesleben|sex|affäre|verliebt|divorce|relationship",
    "law": r"anwalt|anwält|gericht|polizei|strafanzeige|strafe\b|vorstrafe|lawyer|police|court",
}
PATTERNS = {
    "email": re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b"),
    "phone": re.compile(r"(?:\+|00)\d{2}[\s/.-]?\d{2}[\s/.-]?\d{3}[\s/.-]?\d{2}[\s/.-]?\d{2}|\b0\d{2}[\s/.-]?\d{3}[\s/.-]?\d{2}[\s/.-]?\d{2}\b"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b"),
    "ahv": re.compile(r"\b756[.\s]?\d{4}[.\s]?\d{4}[.\s]?\d{2}\b"),
    "address": re.compile(r"(?i)\b[\wäöü-]+(?:strasse|straße|str\.|weg|gasse|platz|allee|ring)\s+\d+[a-z]?\b"),
}
_KEYWORD_RX = {k: re.compile(rf"(?i)\b(?:{v})") for k, v in KEYWORDS.items()}


@dataclass(frozen=True)
class PrivacyConfig:
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:8082"   # decision-embed on the router host (POST /privacy)
    timeout_s: float = 2.0
    threshold: float | None = None             # None = the classifier's own threshold (chosen for recall at training)
    terms: tuple = ()                          # operator terms: names, places - whole words, case-insensitive
    keywords: bool = True                      # built-in keyword groups (KEYWORDS) on
    redact_domains: tuple = ("person", "device_tracker", "zone", "calendar", "todo", "lock", "alarm_control_panel",
                             "camera", "image")
    redact_device_classes: tuple = ("motion", "occupancy", "presence", "door", "window", "opening", "garage_door",
                                    "lock", "tamper")
    redact_names: tuple = ()                   # entity names (substring, case-insensitive) removed from tool results
    redact_tools: tuple = ("todo_get_items",)   # tools whose whole result is removed (a list's items)
    refusal: str = DEFAULT_REFUSAL
    terms_rx: re.Pattern | None = field(default=None, compare=False)


FIELDS = tuple(k for k in PrivacyConfig.__dataclass_fields__ if k != "terms_rx")


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
    if not isinstance(v["refusal"], str) or not v["refusal"].strip():
        return invalid("router.cloud.privacy_check.refusal must be a non-empty sentence")
    rx = re.compile(r"(?i)(?<![\wäöü])(?:" + "|".join(re.escape(x) for x in v["terms"]) + r")(?![\wäöü])") if v["terms"] else None
    return PrivacyConfig(**{**v, "timeout_s": float(t), "terms_rx": rx})


@dataclass
class Verdict:
    allowed: bool
    result: str            # passed | personal | unavailable
    by: str = ""           # rules:<group> | classifier
    probability: float | None = None
    redacted: int = 0
    body: dict | None = None   # redacted copy to send (allowed only)

    def journal(self):
        return {"result": self.result, "by": self.by or None, "p": None if self.probability is None else round(self.probability, 3),
                "redacted": self.redacted}


def user_texts(body):
    """What the user wrote: the text of every user message (chat) or the prompt (generate)."""
    out = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [p.get("text") for p in c if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if isinstance(body.get("prompt"), str):
        out.append(body["prompt"])
    return [t for t in out if t.strip()]


def rule_hit(texts, cfg):
    """The first rule that matches what the user wrote, or None."""
    for t in texts:
        if cfg.terms_rx is not None and cfg.terms_rx.search(t):
            return "terms"
        for name, rx in PATTERNS.items():
            if rx.search(t):
                return name
        if cfg.keywords:
            for name, rx in _KEYWORD_RX.items():
                if rx.search(t):
                    return name
    return None


def _entry_hidden(block, cfg):
    """One live-context entry ("- names: ...\\n  domain: ...\\n  attributes: ...") to be removed?"""
    m = re.search(r"(?m)^\s+domain:\s*(\S+)", block)
    if m and m.group(1) in cfg.redact_domains:
        return True
    m = re.search(r"(?m)^\s+device_class:\s*(\S+)", block)
    if m and m.group(1) in cfg.redact_device_classes:
        return True
    names = re.match(r"-\s*names:\s*(.*)", block)
    low = (names.group(1) if names else "").lower()
    return any(n.lower() in low for n in cfg.redact_names)


def redact_context(text, cfg):
    """Remove hidden entries from a live-context text; (text, number removed). Text without entries stays as it is."""
    parts = re.split(r"(?m)^(?=- names:)", text)
    if len(parts) < 2:
        return text, 0
    kept = [p for p in parts[1:] if not _entry_hidden(p, cfg)]
    return parts[0] + "".join(kept), len(parts) - 1 - len(kept)


def _redact_tool_content(content, cfg):
    """Tool result as Home Assistant sends it: JSON with a `result` text, or plain text."""
    if not isinstance(content, str):
        return content, 0
    try:
        data = json.loads(content)
    except ValueError:   # exception-ok: a plain-text tool result is valid; json has no "is this JSON" check
        data = None
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        new, n = redact_context(data["result"], cfg)
        if n:
            data["result"] = new
            return json.dumps(data, ensure_ascii=False), n
        return content, 0
    return redact_context(content, cfg)


def redact_body(body, cfg):
    """Copy of the request with the house context redacted; (copy, entries removed)."""
    out = copy.deepcopy(body)
    removed = 0
    for m in out.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        if m.get("tool_name") in cfg.redact_tools or m.get("name") in cfg.redact_tools:
            m["content"] = REDACTED
            removed += 1
            continue
        m["content"], n = _redact_tool_content(m.get("content"), cfg)
        removed += n
    return out, removed


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
    redacted, n = redact_body(body, cfg)
    return Verdict(True, "passed", by="classifier" if texts else "", probability=p, redacted=n, body=redacted)
