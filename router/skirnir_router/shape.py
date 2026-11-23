"""Response shapes: how the relay (proxy._Relay) writes an Ollama-format answer in the client's format.

Every request has a shape (ADR-0120). `OllamaShape` is the native format (Ollama's own NDJSON, unchanged); the OpenAI
shapes live in openai_api.py, the Anthropic shape in anthropic_api.py. The relay calls every method unconditionally; a
format that has no use for a step inherits the default here, so the relay needs neither `hasattr` nor a `shape is None`
branch.

A shape is created per request and may keep state across the chunks of one answer (ids, open blocks).
"""

import json
from abc import ABC, abstractmethod

from aiohttp import web

from .common import ollama_error

NDJSON = "application/x-ndjson"


class Shape(ABC):
    """Contract between the relay and a client format. Subclasses define `name`, `error`, `complete` and `chunk`; the
    rest has defaults that write nothing extra."""

    name = "?"                          # `via` in the route journal, metrics and fallback observation
    stream_content_type = NDJSON        # Content-Type of a streamed answer
    keepalive_s = 0                     # > 0: pings while the node has not answered yet (proxy._Relay._connect)
    verbatim = False                    # the client speaks Ollama's own format: unparsable upstream bytes reach it unchanged
    reports_abort_in_stream = False     # a network abort after the first byte ends with an error event, not HTTP 502

    @staticmethod
    @abstractmethod
    def error(status, msg, why=None):
        """Error response before the first byte (web.Response in the client's format)."""

    @abstractmethod
    def complete(self, j):
        """Unary answer: Ollama JSON object -> response body (bytes)."""

    @abstractmethod
    def chunk(self, j):
        """One Ollama stream chunk -> bytes for the client (may be empty)."""

    def bind(self, request_id, model):
        """Before the first byte: the request id and the model name the client knows. Default: not needed."""
        return None

    def head(self):
        """Bytes right after the stream opens (Anthropic: message_start). Default: none."""
        return b""

    def ping(self):
        """Keep-alive event while waiting for the node; only called when `keepalive_s` > 0. Default: none."""
        return b""

    def tail(self):
        """Bytes after the last chunk (OpenAI: [DONE]). Default: none."""
        return b""

    def stream_error(self, status, msg, why=None):
        """Error event inside an open stream. Default: none (the format has no error event)."""
        return b""

    def upstream_error_line(self, line, status, msg):
        """Ollama aborted the answer mid-stream with an `{"error": ...}` line: what the client receives instead."""
        return self.stream_error(status, msg)

    def raw_line(self, line):
        """A stream line that is not JSON: forwarded only to a client that speaks Ollama's format."""
        return line if self.verbatim else b""

    def client_error(self, status, msg, headers):
        """Error the router decides on after choosing the node (cloud 4xx, server refusal, managed-turn problem)."""
        return self.error(status, msg)

    def pass_through(self, status, raw, headers):
        """A node's 4xx/501 answer that goes to the client: its error message in the client's format."""
        try:
            parsed = json.loads(raw)
        except ValueError:  # exception-ok: upstream data; json.loads is the only full validity check
            parsed = None
        msg = (parsed.get("error") if isinstance(parsed, dict) else None) or raw.decode(errors="replace")
        return self.error(status, str(msg))


class OllamaShape(Shape):
    """Ollama's native format: the answer as Ollama wrote it, only `model` (and `routing`) set by the relay. Unparsable
    lines and Ollama's own error lines reach the client verbatim, as from Ollama itself."""
    name = "ollama"
    verbatim = True
    error = staticmethod(ollama_error)

    def complete(self, j):
        return json.dumps(j).encode()

    def chunk(self, j):
        return (json.dumps(j) + "\n").encode()

    def stream_error(self, status, msg, why=None):
        return (json.dumps({"error": msg}) + "\n").encode()

    def upstream_error_line(self, line, status, msg):
        return line if line.endswith(b"\n") else line + b"\n"

    def client_error(self, status, msg, headers):
        return web.Response(status=status, body=json.dumps({"error": msg}).encode(), content_type="application/json", headers=headers)

    def pass_through(self, status, raw, headers):
        return web.Response(status=status, body=raw, content_type="application/json", headers=headers)
