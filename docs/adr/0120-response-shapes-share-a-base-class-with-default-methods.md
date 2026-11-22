---
status: proposed
date: 2026-11-23
decision-makers: maintainer
---

# Response shapes share a base class with default methods

> Builds on [ADR-0022](0022-components-report-state-as-facts.md) (components report state as facts). First seam of the
> router's portability work; the planned backend protocol for nodes and providers is a separate decision.

## Context and Problem Statement

The relay (`proxy._Relay`) receives every answer in Ollama's format and writes it in the client's format. The OpenAI
and Anthropic formats were "shapes" (`OpenAIChatShape`, `OpenAICompletionShape`, `OpenAIEmbedShape`, `AnthropicShape`)
without a common type; the native Ollama format had no shape at all (`shape=None`). The relay therefore asked
`hasattr(self.shape, "head" | "bind" | "stream_error")`, `getattr(self.shape, "keepalive_s", 0)` and
`if self.shape:` / `if not self.shape:` in about twenty places to find out what a format can do. The capabilities
of a format were inferred from the presence of an attribute, the native format was a special case at every step, and
a new format (or a new optional step) meant finding every place again. `hasattr(..., "stream_error")` also carried two
different meanings: "has an error event" and "a network abort after the first byte is reported in the stream instead
of ending with HTTP 502".

## Decision Drivers

* Report facts, do not infer them (ADR-0022): a format states what it does instead of the relay probing attributes.
* No behaviour change on the wire for any client format, byte for byte.
* Small and local: shapes stay where they are (`openai_api.py`, `anthropic_api.py`), the relay keeps its structure.

## Considered Options

* **An abstract base class `Shape` with default methods, and `OllamaShape` for the native format.**
* A `typing.Protocol` without implementations. Rejected: it removes neither the `hasattr` checks (an optional method
  is still optional) nor the native special case; the defaults would have to live in the relay again.
* Keep `shape=None` for the native format and only add defaults to the foreign shapes. Rejected: the relay keeps a
  `shape is None` branch at every step, which is the larger half of the problem.

## Decision Outcome

Chosen option: an abstract base class in `router/skirnir_router/shape.py`.

* Required: `name`, `error(status, msg, why=None)`, `complete(j)`, `chunk(j)`.
* Defaults that write nothing extra: `bind()`, `head()`, `ping()`, `tail()`, `stream_error()`, `keepalive_s = 0`,
  `stream_content_type` NDJSON.
* Two facts instead of attribute probing: `verbatim` (the client speaks Ollama's own format, so unparsable upstream
  bytes and Ollama's own error lines reach it unchanged) and `reports_abort_in_stream` (a network abort after the first
  byte ends with an error event; otherwise the handler ends with HTTP 502, as the native format and embeddings always
  did).
* Overridable steps for what differed between native and foreign formats: `raw_line()`, `upstream_error_line()`,
  `client_error()` and `pass_through()` (a node's 4xx: raw body with routing headers for native clients, the message in
  the client's error format otherwise).
* `OllamaShape` implements the native format exactly as the former `shape is None` branches did. `route_request` keeps
  `shape=None` as its default; the relay substitutes `OllamaShape()`, so callers are unchanged.
* The relay writes `head()` and `tail()` only when they are non-empty: aiohttp flushes buffered headers on an empty
  write, which a format without head or tail never caused before.

### Consequences

* Good: the relay calls every shape method unconditionally; no `hasattr`, `getattr` or `shape is None` on the shape
  remains in `proxy.py`.
* Good: a new client format is one subclass; forgetting a required method fails at instantiation, not mid-stream.
* Good: the two meanings of the former `hasattr(..., "stream_error")` are separate and named.
* Bad: the native format keeps its historical asymmetry (an abort after the first byte ends with HTTP 502 although
  `stream_error` would have an NDJSON error line). That is now visible as a fact; changing it is a behaviour change
  for another decision.

### Confirmation

`test/selftest_shape.py` pins the defaults, the facts of every format, and runs the router app against a synthetic
Ollama upstream: a native client receives exactly the same bytes as before for unary and streamed answers, unparsable
lines, Ollama's error line and a node's 4xx. The same wire tests pass against the code before this change. The
end-to-end suite covers the native, OpenAI, Anthropic and cloud paths.

## More Information

Router 0.37.2. The `_Relay` also shares the end of a relayed stream (`_close_stream`) between the Ollama node and the
translated backends (cloud, OpenAI-compatible node). The unary completion of the translated backends is deliberately
not merged: only the OpenAI-compatible node commits a managed turn there, and whether a cloud request can ever carry
one is not provable from the relay alone, so merging could change behaviour. It belongs to the backend protocol.
