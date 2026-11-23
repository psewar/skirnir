"""Context redactors: remove house context from tool results before a request leaves for a cloud tier (router 0.42.0,
ADR-0119, ADR-0125).

The content check (privacy.py) hands every tool result of an allowed request to the redactors named in
`router.cloud.privacy_check.redactors`, in that order. Each knows one tool format:

* `ha_assist` (default): Home Assistant Assist. A tool result is JSON with a `result` text or plain text; live-context
  entries (`- names: ...` blocks) of configured domains, device classes and names are removed. The code of router 0.13.0
  up to 0.42.0, moved here unchanged.
* `generic_json`: any tool whose result is JSON. `generic_json.tools` names the tools (or `*` for every JSON result) and
  the dotted paths to remove from their result; `generic_json.keys` removes keys of that name at any depth.

A redactor reports facts, not exceptions: what it removed and whether it recognised the format (`Redaction`). A result
of a tool named in `generic_json.tools` that is not JSON cannot be redacted and keeps the request local
(`unredactable`). Whether a result that no redactor recognises passes (default, as before) or keeps the request local is
the content check's decision (`unrecognised_tool_results`, ADR-0125).

Formats of other home automation systems (openHAB, ioBroker) are future redactors; they are added only together with
test data of the real format, never guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Redaction:
    content: object
    removed: int = 0               # entries, keys or elements removed
    recognised: bool = False       # this redactor knows the format (whether or not it removed anything)
    unredactable: bool = False     # the result is this redactor's to redact, but it cannot: the request stays local


class ContextRedactor(Protocol):
    name: str

    def redact(self, tool_names: tuple, content) -> Redaction:
        """Redact one tool result. tool_names: the names the message carries (`tool_name`, `name`)."""


TEXT, JSON, TOO_DEEP = "text", "json", "too_deep"
MAX_JSON_DEPTH = 100   # generic_json walks results up to this nesting; deeper ones cannot be redacted


def _parse(content):
    """A tool result's JSON value and what it is: (value, JSON), (None, TEXT) for plain text, (None, TOO_DEEP) for JSON
    nested beyond what the parser handles."""
    try:
        return json.loads(content), JSON
    except RecursionError:   # exception-ok: the nesting depth is only known by parsing; too deep to inspect = cannot redact
        return None, TOO_DEEP
    except ValueError:   # exception-ok: a plain-text tool result is valid; json has no "is this JSON" check
        return None, TEXT


def _deeper_than(value, limit):
    """Is the JSON value nested deeper than limit? Iterative, so a deep value cannot exhaust the stack here."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (dict, list)):
            if depth > limit:
                return True
            stack.extend((c, depth + 1) for c in (node.values() if isinstance(node, dict) else node))
    return False


# --- ha_assist: Home Assistant Assist (router 0.13.0 up to 0.42.0, unchanged) ---

def _entry_hidden(block, domains, device_classes, names_hidden):
    """One live-context entry ("- names: ...\\n  domain: ...\\n  attributes: ...") to be removed?"""
    m = re.search(r"(?m)^\s+domain:\s*(\S+)", block)
    if m and m.group(1) in domains:
        return True
    m = re.search(r"(?m)^\s+device_class:\s*(\S+)", block)
    if m and m.group(1) in device_classes:
        return True
    names = re.match(r"-\s*names:\s*(.*)", block)
    low = (names.group(1) if names else "").lower()
    return any(n.lower() in low for n in names_hidden)


def _split_entries(text):
    return re.split(r"(?m)^(?=- names:)", text)


def redact_context(text, domains, device_classes, names_hidden):
    """Remove hidden entries from a live-context text; (text, number removed). Text without entries stays as it is."""
    parts = _split_entries(text)
    if len(parts) < 2:
        return text, 0
    kept = [p for p in parts[1:] if not _entry_hidden(p, domains, device_classes, names_hidden)]
    return parts[0] + "".join(kept), len(parts) - 1 - len(kept)


@dataclass(frozen=True)
class HaAssistRedactor:
    domains: tuple
    device_classes: tuple
    names: tuple
    name: str = "ha_assist"

    def redact(self, tool_names, content):
        """Tool result as Home Assistant sends it: JSON with a `result` text, or plain text. Recognised: the JSON envelope
        with a `result` text, or text that carries live-context entries. JSON nested too deeply to parse cannot be
        inspected and keeps the request local (until router 0.42.0 it failed the request with an internal error)."""
        if not isinstance(content, str):
            return Redaction(content)
        data, kind = _parse(content)
        if kind == TOO_DEEP:
            return Redaction(content, unredactable=True)
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            new, n = redact_context(data["result"], self.domains, self.device_classes, self.names)
            if n:
                data["result"] = new
                return Redaction(json.dumps(data, ensure_ascii=False), n, recognised=True)
            return Redaction(content, 0, recognised=True)
        new, n = redact_context(content, self.domains, self.device_classes, self.names)
        return Redaction(new, n, recognised=len(_split_entries(content)) >= 2)


# --- generic_json: configured paths and keys of any JSON tool result ---

ANY_TOOL = "*"


def _drop_path(node, segments):
    """Remove what the dotted path names below node; number of values removed. `*` stands for every key of an object
    and every element of a list; other segments name an object key. A path that does not exist removes nothing."""
    head, rest = segments[0], segments[1:]
    if isinstance(node, dict):
        keys = list(node) if head == ANY_TOOL else [head] if head in node else []
        if not rest:
            for k in keys:
                del node[k]
            return len(keys)
        return sum(_drop_path(node[k], rest) for k in keys)
    if isinstance(node, list) and head == ANY_TOOL:
        if not rest:
            n = len(node)
            node.clear()
            return n
        return sum(_drop_path(item, rest) for item in node)
    return 0


def _drop_keys(node, keys):
    """Remove every object key named in keys, at any depth; number removed."""
    n = 0
    if isinstance(node, dict):
        for k in [k for k in node if k in keys]:
            del node[k]
            n += 1
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return 0
    return n + sum(_drop_keys(c, keys) for c in children)


@dataclass(frozen=True)
class GenericJsonRedactor:
    tools: tuple    # ((tool name or "*", (dotted path, ...)), ...)
    keys: tuple     # key names removed at any depth
    name: str = "generic_json"

    def redact(self, tool_names, content):
        rules = dict(self.tools)
        named = [rules[n] for n in tool_names if n in rules]
        paths = [p for group in named for p in group] + list(rules.get(ANY_TOOL, ()))
        if not named and ANY_TOOL not in rules:
            return Redaction(content)
        data, kind = _parse(content) if isinstance(content, str) else (None, TEXT)
        if kind == TOO_DEEP or (isinstance(data, (dict, list)) and _deeper_than(data, MAX_JSON_DEPTH)):
            return Redaction(content, unredactable=True)   # JSON this redactor should walk but cannot
        if not isinstance(data, (dict, list)):
            # a tool the operator named must be redacted; one only covered by "*" is simply not this redactor's format
            return Redaction(content, unredactable=bool(named))
        removed = sum(_drop_path(data, p.split(".")) for p in paths)
        removed += _drop_keys(data, frozenset(self.keys)) if self.keys else 0
        if removed:
            return Redaction(json.dumps(data, ensure_ascii=False), removed, recognised=True)
        return Redaction(content, 0, recognised=True)


REDACTORS = ("ha_assist", "generic_json")
