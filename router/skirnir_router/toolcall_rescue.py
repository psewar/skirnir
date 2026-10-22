"""Recovers tool calls that the model wrote in the wrong dialect.

Ollama does not constrain the `tools` path: it generates freely and parses afterwards.
If parsing fails, the call lands as text in `message.content` and is lost to the
client - silently, with `done_reason: "stop"` and no error message
(ollama/ollama#17274). llama.cpp enforces a grammar at this point and cannot
encounter this case; Ollama cannot, so we clean up afterwards.

Measured on 2026-09-15 (local-toolcall-context series): `qwen3-coder:30b`
loses **80 %** of its calls above ~12k prompt tokens this way - even though
tool names and arguments were correct every time. `gemma4:26b`, `glm-4.7-flash`,
`granite4.2:8b`, and `qwen3.6:35b-a3b` were error-free in the same measurement. This tier
is therefore not for the normal case, but for the one outlier - and for the
next one we do not know yet.

PRINCIPLE: better a lost call than a fabricated one. A falsely rescued
call executes a tool the client did not want - at home, that means a
shutter, a lock, or a shell command. Therefore, this tier only acts under strict
conditions and leaves everything unchanged in case of doubt.
"""
import json
import re

from . import values
from .outcome import failed

INTEGER_TEXT = re.compile(r"\s*[+-]?\d+\s*")

# The call must make up the majority of the response. A model that first
# writes a paragraph and then mentions something call-like did not intend a call.
MIN_SHARE = 0.8

# Buffer limit in streaming: longer than this is no longer a tool call, but prose.
MAX_BUFFER = 8192

# qwen3-coder-Dialekt: <function=name><parameter=key>wert</parameter></function>
RE_XML_FN = re.compile(r"<function\s*=\s*([A-Za-z_][\w.-]*)\s*>(.*?)</function\s*>", re.I | re.S)
RE_XML_PARAM = re.compile(r"<parameter\s*=\s*([A-Za-z_][\w.-]*)\s*>(.*?)</parameter\s*>", re.I | re.S)
# Mantles that wrap the call in different families. They do not count as prose,
# otherwise a clean call would fail the ratio rule just because of its packaging.
RE_WRAPPER = re.compile(r"</?\s*(tool_call|tool_calls|tools|function_call)\s*>|"
                       r"<\|/?\s*(tool_call|python_tag)\s*\|>|\[/?TOOL_CALLS?\]", re.I)
# How we detect in the stream that a call MIGHT be coming (prefix, not full view)
RE_CANDIDATE = re.compile(r"^\s*(<\s*t|<\s*f|\{|\[TOOL|<\|tool)", re.I)


def tool_names(tools):
    """The names the client actually offered. Anything else is fabricated."""
    out = set()
    for t in tools or ():
        f = (t or {}).get("function") or {}
        if f.get("name"):
            out.add(str(f["name"]))
    return out


def _schema(tools, name):
    for t in tools or ():
        f = (t or {}).get("function") or {}
        if f.get("name") == name:
            return ((f.get("parameters") or {}).get("properties")) or {}
    return {}


def _fit_type(val, typ):
    """XML delivers everything as text. A `number` parameter as a string causes some
    clients to crash, so type it correctly once - but only if it works."""
    if typ == "integer":
        return int(val) if isinstance(val, str) and INTEGER_TEXT.fullmatch(val) else val
    if typ == "number":
        num = values.number(val, "argument")
        return val if failed(num) else num
    if typ == "boolean":
        if str(val).strip().lower() in ("true", "ja", "1"):
            return True
        if str(val).strip().lower() in ("false", "nein", "0"):
            return False
    return val


def extract(text, allowed, tools=None):
    """(name, arguments) from a call that landed as text - or (None, None).

    `allowed` is the set of tool names offered by the client. A hit
    outside of this is discarded: that would be a hallucinated call, and we
    do not want to pass it through just because it looks good.
    """
    if not text or not allowed:
        return None, None
    raw = text.strip()
    if not raw:
        return None, None
    # The standard for "was the response nothing but a call" is the text WITHOUT the mantle.
    payload_len = max(len(RE_WRAPPER.sub("", raw).strip()), 1)

    m = RE_XML_FN.search(raw)
    if m and m.group(1) in allowed:
        if len(m.group(0)) < MIN_SHARE * payload_len:
            return None, None
        props = _schema(tools, m.group(1))
        args = {}
        for k, v in RE_XML_PARAM.findall(m.group(2)):
            typ = ((props.get(k) or {}).get("type"))
            args[k] = _fit_type(v.strip(), typ)
        return m.group(1), args

    # JSON with a real parser instead of regex: arguments are nested, and a
    # regex that runs to the first closing bracket truncates them.
    dec = json.JSONDecoder()
    i = raw.find("{")
    while i >= 0:
        try:
            o, end = dec.raw_decode(raw, i)
        except ValueError:  # exception-ok: raw_decode reports "no JSON object starts here" only so; scanning model output
            i = raw.find("{", i + 1)
            continue
        if isinstance(o, dict) and o.get("name") in allowed:
            if (end - i) < MIN_SHARE * payload_len:
                return None, None
            args = o.get("arguments")
            if args is None:
                args = o.get("parameters")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:  # exception-ok: the model's arguments are untrusted JSON; whether they parse shows only the parser
                    args = {}
            return o["name"], (args if isinstance(args, dict) else {})
        i = raw.find("{", i + 1)

    return None, None


def rescue_message(msg, tools):
    """Lifts a call stranded in text into `message.tool_calls`.

    Returns the rescued name (for metrics and logging) or None.
    Touches the message only if it TRULY has no tool_calls - a
    response containing both is already fine and is not our concern.
    """
    if not isinstance(msg, dict) or msg.get("tool_calls"):
        return None
    allowed = tool_names(tools)
    if not allowed:
        return None
    name, args = extract(msg.get("content") or "", allowed, tools)
    if not name:
        return None
    msg["tool_calls"] = [{"function": {"name": name, "arguments": args or {}}}]
    msg["content"] = ""
    return name


def suspicious(text):
    """Looked like a call but could not be extracted.

    This is the case this tier does NOT fix - a dialect we do not know,
    or a tool that was not offered at all. It remains lost to the client,
    but should not remain silent: only when this counter moves does the
    next expansion tier (forced schema as a second attempt) make sense. Before that, it would be
    code against a guess.
    """
    if not text:
        return False
    raw = text.strip()
    return bool(raw) and bool(RE_CANDIDATE.match(raw)) and ("function" in raw or "name" in raw
                                                           or "tool" in raw.lower())


def active_for(body):
    """When this tier is even responsible.

        Not with `format`: there Ollama already constrains the output via grammar,
    a JSON response is then the intended result and not a failed call.
    """
    return bool((body or {}).get("tools")) and not (body or {}).get("format")


class StreamRescuer:
    """Same for the stream - where it is harder because the text is at the client
    before we know it was a call.

    Therefore: as long as the current text can still BECOME a call, we hold it
    back. Once it is clear that it is prose, we release the held content in one go
    and never interfere again. We only delay what would have been lost anyway.
    """

    def __init__(self, tools):
        self.allowed = tool_names(tools)
        self.tools = tools
        self.buffer = ""
        self.holding = bool(self.allowed)   # do not start at all without tools
        self.rescued = None
        self.lost = False   # looked like a call but could not be extracted

    def chunk(self, j):
        """Takes an upstream chunk, returns the chunks that are allowed out."""
        if not self.holding:
            return [j]
        msg = j.get("message") or {}
        if msg.get("tool_calls"):        # Ollama hat es doch selbst geschafft
            self.holding = False
            return self._release(j)
        piece = msg.get("content") or ""
        if piece:
            # Check first, THEN buffer - otherwise _release counts this piece twice.
            pending = self.buffer + piece
            if not RE_CANDIDATE.match(pending) or len(pending) > MAX_BUFFER:
                self.holding = False   # just prose: deliver all held content
                return self._release(j)
            self.buffer = pending
            j = dict(j)
            j["message"] = dict(msg, content="")   # Inhalt halten, Rest (done, Zaehler) durchlassen
        if j.get("done"):
            return self.finish(j)
        return [] if piece else [j]

    def _release(self, j):
        """Push held text (including this chunk's) before the current chunk."""
        msg = j.get("message") or {}
        text = self.buffer + (msg.get("content") or "")
        self.buffer = ""
        if not text:
            return [j]
        # Deliberately minimal: the counters (eval_count & co.) stay on the final chunk,
        # otherwise perf_record miscounts on a chunk that is not an end.
        head = {"model": j.get("model"), "message": {"role": "assistant", "content": text},
                  "done": False}
        if j.get("created_at"):
            head["created_at"] = j["created_at"]
        rest = dict(j)
        rest["message"] = dict(msg, content="")
        return [head, rest]

    def finish(self, j):
        """Last chunk: either it was a call, or it was text after all."""
        self.holding = False
        if self.buffer:
            name, args = extract(self.buffer, self.allowed, self.tools)
            if name:
                self.rescued = name
                self.buffer = ""
                j = dict(j)
                j["message"] = {"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": name, "arguments": args or {}}}]}
                return [j]
            # not extractable: the text goes out as text (better than swallowing),
            # but the incident is reported instead of staying silent.
            self.lost = suspicious(self.buffer)
        return self._release(j)
