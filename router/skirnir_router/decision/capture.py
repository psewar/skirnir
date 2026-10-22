"""Collect operational training data (Section 9): JSONL in Jevlike format plus provenance.

Two label sources:
  * `client`: the client explicitly requested a specific role (standard/gross/assist/code). This is the label - real,
    free, but biased by the client (HA almost always requests assist).
  * `engine`: the auto-role was decided by an engine. This is a pseudo-label and marked as such; it is only usable
    as training data after review.
Privacy: only for clients listed in `capture.clients` (opt-in), context truncated to `max_context_chars` and
anonymized (keys/passwords like in the Cloud-Credential-Scan, email, IP, long numbers, URLs). `group` is a hash
of the normalized first words, so decision-eval does not distribute similar requests across train/validation/test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

from ..common import log

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
URL = re.compile(r"https?://\S+")
LONG_NUMBER = re.compile(r"\b\d{6,}\b")


def anonymize(text: str) -> str:
    from .. import cloud   # late: cloud imports a lot; SECRET_PATTERNS is the same list as in the Credential-Scan
    for _name, rx in cloud.SECRET_PATTERNS:
        text = rx.sub("<secret>", text)
    text = URL.sub("<url>", text)
    text = EMAIL.sub("<email>", text)
    text = IPV4.sub("<ip>", text)
    return LONG_NUMBER.sub("<number>", text)


def group_id(text: str) -> str:
    words = re.findall(r"\w+", text.lower())[:8]
    return hashlib.sha1(" ".join(words).encode()).hexdigest()[:12]


class Capture:
    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled"))
        self.path = cfg.get("path") or "/var/lib/skirnir-router/decisions.jsonl"
        self.clients = set(cfg.get("clients") or [])
        self.anonymize = bool(cfg.get("anonymize", True))
        self.max_chars = int(cfg.get("max_context_chars") or 2000)

    def allowed(self, client: str | None) -> bool:
        return self.enabled and bool(client) and client in self.clients

    def record(self, context: str, options: list[str], label: str | None, source: str, client: str | None, request_id: str,
               decision: dict | None = None):
        """Append a line; write errors must never disturb the request."""
        if not self.allowed(client) or label not in options:
            return
        ctx = context[: self.max_chars]
        if self.anonymize:
            ctx = anonymize(ctx)
        row = {"context": ctx, "options": list(options), "label": options.index(label), "label_source": source, "client": client,
               "group": group_id(ctx), "request_id": request_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if decision:
            row["decision"] = decision
        try:
            if os.path.dirname(self.path):
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:  # exception-ok: file system boundary; a lost capture row never fails the request
            log.warning("decision capture: %s", e)
