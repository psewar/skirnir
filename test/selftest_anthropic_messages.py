#!/usr/bin/env python3
"""Selbsttest der Anthropic-Messages-API (/v1/messages): Uebersetzung Anthropic -> Ollama /api/chat, Antwortform,
SSE-Ereignisfolge, Fehlerformen und Anmeldung per x-api-key/Bearer. Laeuft ohne Router und ohne Netz.

Anlass 2026-10-01: ein Claude-Code-Agent sprach Ollamas eigenes /v1/messages, am Router vorbei.
Ollama kennt dort kein num_ctx, lud das Modell mit seinem Default-Kontext neu und verdraengte den Runner aller anderen
Clients. Die Ereignisfolge im Stream muss exakt stimmen: Claude Code setzt Bloecke anhand von Index und Typ zusammen.
"""
import json
import os
import sys
from types import SimpleNamespace

from multidict import CIMultiDict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import anthropic_api as A   # noqa: E402
from skirnir_router import auth, state   # noqa: E402
from skirnir_router.common import anthropic_error   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def events(raw):
    """SSE-Bytes -> [(event, data)]; prueft nebenbei, dass jede Zeile 'event:' und 'data:' als Paar kommt."""
    out = []
    for frame in raw.decode().split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        assert lines[0].startswith("event: ") and lines[1].startswith("data: "), frame
        ev, data = lines[0][7:], json.loads(lines[1][6:])
        assert data["type"] == ev, (ev, data)
        out.append((ev, data))
    return out


def kinds(evs):
    """Kurzform fuer Vergleiche: content_block_start:text, content_block_delta:text_delta, ..."""
    out = []
    for ev, d in evs:
        if ev == "content_block_start":
            out.append(f"start:{d['content_block']['type']}@{d['index']}")
        elif ev == "content_block_delta":
            out.append(f"delta:{d['delta']['type']}@{d['index']}")
        elif ev == "content_block_stop":
            out.append(f"stop@{d['index']}")
        else:
            out.append(ev)
    return out


TOOLS = [{"name": "read_file", "description": "Read a file", "input_schema": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]


def t_anfrage():
    b = {"model": "code:latest", "max_tokens": 1024, "temperature": 0.2, "top_k": 20, "stop_sequences": ["END"],
         "system": [{"type": "text", "text": "Du bist ein Agent.", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "Arbeite sorgfaeltig."}],
         "messages": [{"role": "user", "content": "Hallo"}], "metadata": {"user_id": "geheim"}}
    n, err = A.to_native(b)
    check("einfacher Text: kein Fehler", err is None, err)
    check("System-Liste -> eine system-Nachricht, cache_control weg",
          n["messages"][0] == {"role": "system", "content": "Du bist ein Agent.\n\nArbeite sorgfaeltig."}, n["messages"][0])
    check("user-String -> content", n["messages"][1] == {"role": "user", "content": "Hallo"})
    check("max_tokens/temperature/top_k/stop -> options",
          n["options"] == {"num_predict": 1024, "temperature": 0.2, "top_k": 20, "stop": ["END"]}, n["options"])
    check("ohne thinking -> think false", n["think"] is False)
    check("metadata.user_id geht nicht an Ollama", "metadata" not in n and "geheim" not in json.dumps(n))
    check("stream fehlt -> false", n["stream"] is False)

    n, _ = A.to_native({**b, "thinking": {"type": "enabled", "budget_tokens": 4000}, "stream": True})
    check("thinking enabled -> think true, stream true", n["think"] is True and n["stream"] is True)
    n, _ = A.to_native({**b, "thinking": {"type": "disabled"}})
    check("thinking disabled -> think false", n["think"] is False)

    img = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}},
        {"type": "text", "text": "Was siehst du?"}]}]}
    n, err = A.to_native(img)
    check("Bild base64 -> images", err is None and n["messages"][0] == {"role": "user", "content": "Was siehst du?",
                                                                         "images": ["iVBORw0K"]}, n and n["messages"])
    bad = json.loads(json.dumps(img))
    bad["messages"][0]["content"][0]["source"] = {"type": "url", "url": "https://example.com/a.png"}
    n, err = A.to_native(bad)
    check("Bild per URL -> 400-Klartext", n is None and "only base64" in err, err)

    n, err = A.to_native({"model": "m", "max_tokens": 10, "tools": TOOLS, "messages": [{"role": "user", "content": "x"}]})
    check("tools -> Ollama-Funktionen", n["tools"] == [{"type": "function", "function": {
        "name": "read_file", "description": "Read a file", "parameters": TOOLS[0]["input_schema"]}}], n["tools"])
    n, _ = A.to_native({"model": "m", "max_tokens": 10, "tools": TOOLS + [{"type": "web_search_20250305", "name": "web_search"}],
                        "messages": [{"role": "user", "content": "x"}]})
    check("Server-Werkzeug ohne Schema faellt weg", [t["function"]["name"] for t in n["tools"]] == ["read_file"])
    n, _ = A.to_native({"model": "m", "max_tokens": 10, "tools": TOOLS, "tool_choice": {"type": "none"},
                        "messages": [{"role": "user", "content": "x"}]})
    check("tool_choice none -> keine tools", "tools" not in n)
    n, _ = A.to_native({"model": "m", "max_tokens": 10, "tools": TOOLS, "tool_choice": {"type": "tool", "name": "read_file"},
                        "messages": [{"role": "user", "content": "x"}]})
    check("tool_choice tool -> Systemhinweis", n["messages"][0]["role"] == "system" and "read_file" in n["messages"][0]["content"])

    for label, body, part in [("ohne model", {"messages": [{"role": "user", "content": "x"}]}, "model is required"),
                              ("ohne messages", {"model": "m"}, "messages is required"),
                              ("falsche Rolle", {"model": "m", "messages": [{"role": "system", "content": "x"}]}, "role must be"),
                              ("unbekannter Block", {"model": "m", "messages": [{"role": "user", "content": [{"type": "audio"}]}]},
                               "not supported")]:
        n, err = A.to_native(body)
        check(f"Validierung {label} -> Klartext", n is None and part in (err or ""), err)


def t_verlauf():
    msgs = [
        {"role": "user", "content": "Lies a.txt und b.txt"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Ich lese beide.", "signature": "abc"},
            {"type": "text", "text": "Ich lese die Dateien."},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}},
            {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "b.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Inhalt A"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "is_error": True,
             "content": [{"type": "text", "text": "nicht gefunden"}]},
            {"type": "text", "text": "Und jetzt?"}]},
        {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "xyz"}, {"type": "text", "text": "Fertig."}]},
    ]
    out, err = A.messages_to_native(msgs)
    check("Verlauf: kein Fehler", err is None, err)
    want = [
        {"role": "user", "content": "Lies a.txt und b.txt"},
        {"role": "assistant", "content": "Ich lese die Dateien.", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
            {"function": {"name": "read_file", "arguments": {"path": "b.txt"}}}]},
        {"role": "tool", "content": "Inhalt A", "tool_name": "read_file"},
        {"role": "tool", "content": "Error: nicht gefunden", "tool_name": "read_file"},
        {"role": "user", "content": "Und jetzt?"},
        {"role": "assistant", "content": "Fertig."},
    ]
    check("tool_use/tool_result-Paare, id -> tool_name, is_error-Praefix", out == want, json.dumps(out, ensure_ascii=False))
    check("thinking/redacted_thinking im Verlauf verworfen", "Ich lese beide." not in json.dumps(out) and "xyz" not in json.dumps(out))

    out, _ = A.messages_to_native([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "x"}]}])
    check("tool_result allein -> nur die tool-Nachricht (ohne leere user-Nachricht), ohne bekannten Namen kein tool_name",
          out == [{"role": "tool", "content": "x"}], out)


def t_antwort():
    s = A.AnthropicShape("code:latest")
    s.bind("r-42", "code:latest")
    j = {"model": "code:latest", "done": True, "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 7,
         "message": {"role": "assistant", "thinking": "Hm.", "content": "Ich lese.", "tool_calls": [
             {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]}}
    m = json.loads(s.complete(j))
    check("Message-ID = msg_<request_id>, model = angefragter Name", m["id"] == "msg_r-42" and m["model"] == "code:latest")
    check("content: thinking (signature leer), text, tool_use",
          [c["type"] for c in m["content"]] == ["thinking", "text", "tool_use"] and m["content"][0]["signature"] == "", m["content"])
    tu = m["content"][2]
    check("tool_use: id toolu_..., name, input als Objekt",
          tu["id"].startswith("toolu_") and tu["name"] == "read_file" and tu["input"] == {"path": "a.txt"}, tu)
    check("stop_reason tool_use, usage aus den Zaehlern",
          m["stop_reason"] == "tool_use" and m["usage"] == {"input_tokens": 120, "output_tokens": 7} and m["stop_sequence"] is None)
    check("done_reason length -> max_tokens", A.stop_reason({"done_reason": "length"}, False) == "max_tokens")
    check("sonst end_turn", A.stop_reason({"done_reason": "stop"}, False) == "end_turn")
    m = json.loads(A.AnthropicShape("m").complete({"done": True, "message": {"role": "assistant", "content": ""}}))
    check("leere Antwort -> content [] und end_turn", m["content"] == [] and m["stop_reason"] == "end_turn")
    m = json.loads(A.AnthropicShape("m").complete({"done": True, "message": {"tool_calls": [
        {"function": {"name": "f", "arguments": "{\"a\": 1}"}}]}}))
    check("Argumente als JSON-Text -> Objekt", m["content"][0]["input"] == {"a": 1})


def stream(chunks, model="code:latest"):
    s = A.AnthropicShape(model)
    s.bind("r-1", model)
    raw = s.head() + b"".join(s.chunk(c) for c in chunks) + s.tail()
    return events(raw)


def done(**kw):
    return {"model": "code:latest", "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 9,
            "message": {"role": "assistant", "content": ""}, **kw}


def part(**msg):
    return {"model": "code:latest", "done": False, "message": {"role": "assistant", "content": "", **msg}}


def t_stream():
    evs = stream([part(content="Hal"), part(content="lo"), done()])
    check("Text: Ereignisfolge", kinds(evs) == ["message_start", "start:text@0", "delta:text_delta@0", "delta:text_delta@0",
                                                "stop@0", "message_delta", "message_stop"], kinds(evs))
    check("message_start: leerer content, model, id", evs[0][1]["message"]["content"] == [] and
          evs[0][1]["message"]["model"] == "code:latest" and evs[0][1]["message"]["id"] == "msg_r-1")
    check("Text-Deltas ergeben den Text", "".join(d["delta"]["text"] for e, d in evs if e == "content_block_delta") == "Hallo")
    md = evs[-2][1]
    check("message_delta nach dem Ende mit echten Zaehlern",
          md["delta"] == {"stop_reason": "end_turn", "stop_sequence": None} and md["usage"] == {"input_tokens": 50, "output_tokens": 9}, md)

    evs = stream([part(thinking="Erst "), part(thinking="denken."), part(content="Antwort"), done()])
    check("Denken + Text: Block wechselt, signature_delta vor dem Stop",
          kinds(evs) == ["message_start", "start:thinking@0", "delta:thinking_delta@0", "delta:thinking_delta@0",
                         "delta:signature_delta@0", "stop@0", "start:text@1", "delta:text_delta@1", "stop@1",
                         "message_delta", "message_stop"], kinds(evs))
    check("thinking-Block startet leer", evs[1][1]["content_block"] == {"type": "thinking", "thinking": ""})

    calls = [{"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
             {"function": {"name": "read_file", "arguments": {"path": "b.txt"}}}]
    evs = stream([part(content="Ich lese."), part(tool_calls=calls), done()])
    check("Text + 2 Tools: fortlaufende Indizes, je Tool ein input_json_delta",
          kinds(evs) == ["message_start", "start:text@0", "delta:text_delta@0", "stop@0",
                         "start:tool_use@1", "delta:input_json_delta@1", "stop@1",
                         "start:tool_use@2", "delta:input_json_delta@2", "stop@2", "message_delta", "message_stop"], kinds(evs))
    starts = [d["content_block"] for e, d in evs if e == "content_block_start" and d["content_block"]["type"] == "tool_use"]
    check("tool_use-Start mit leerem input, eigener id und Namen",
          all(b["input"] == {} and b["id"].startswith("toolu_") and b["name"] == "read_file" for b in starts)
          and starts[0]["id"] != starts[1]["id"])
    pj = [json.loads(d["delta"]["partial_json"]) for e, d in evs if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta"]
    check("partial_json = komplette Argumente", pj == [{"path": "a.txt"}, {"path": "b.txt"}], pj)
    check("stop_reason tool_use", evs[-2][1]["delta"]["stop_reason"] == "tool_use")

    evs = stream([done(message={"role": "assistant", "content": "", "tool_calls": calls[:1]})])
    check("nur Tool (am Abschluss-Chunk)", kinds(evs) == ["message_start", "start:tool_use@0", "delta:input_json_delta@0",
                                                          "stop@0", "message_delta", "message_stop"], kinds(evs))

    evs = stream([part(content="abgebrochen")])
    check("Stream ohne Abschluss-Chunk wird trotzdem sauber beendet",
          kinds(evs)[-3:] == ["stop@0", "message_delta", "message_stop"], kinds(evs))

    s = A.AnthropicShape("m")
    raw = s.head() + s.chunk(part(content="x")) + s.stream_error(503, "GPU busy") + s.tail()
    evs = events(raw)
    check("Fehler mitten im Stream -> event error (overloaded_error), danach nichts mehr",
          evs[-1][0] == "error" and evs[-1][1]["error"]["type"] == "overloaded_error", kinds(evs))
    check("ping-Ereignis", events(A.AnthropicShape.ping()) == [("ping", {"type": "ping"})])


def body_of(resp):
    return json.loads(resp.body)


def t_fehler():
    why = {"code": "gpu_busy", "retry_after_s": 30, "blockers": [{"node": "gpu-desktop", "code": "gpu_busy"}]}
    r = anthropic_error(503, "no node available for model 'code:latest': GPU busy", why)
    b = body_of(r)
    check("kein Platz (503) -> HTTP 529 overloaded_error", r.status == 529 and b["type"] == "error"
          and b["error"]["type"] == "overloaded_error", (r.status, b))
    check("... mit Retry-After und error.skirnir (Grund-Code, Hindernisse)", r.headers.get("Retry-After") == "30"
          and b["error"]["skirnir"]["code"] == "gpu_busy" and b["error"]["skirnir"]["blockers"][0]["node"] == "gpu-desktop")
    for status, typ in [(400, "invalid_request_error"), (401, "authentication_error"), (403, "permission_error"),
                        (404, "not_found_error"), (413, "request_too_large"), (429, "rate_limit_error"), (502, "api_error")]:
        r = anthropic_error(status, "x")
        check(f"{status} -> {typ}", r.status == status and body_of(r)["error"]["type"] == typ)
    check("Fehlerform nach Pfad: /v1/messages Anthropic, /v1/chat OpenAI",
          auth.error_format("/v1/messages") is anthropic_error and auth.error_format("/v1/messages/count_tokens") is anthropic_error
          and auth.error_format("/v1/chat/completions").__name__ == "openai_error" and auth.error_format("/api/chat").__name__ == "ollama_error")


def t_auth():
    import hashlib
    tok = "geheimes-test-token"
    state.CFG = SimpleNamespace(client_auth={"mode": "enforce", "clients": {
        "agent-cli": {"token_sha256": hashlib.sha256(tok.encode()).hexdigest()}}})
    state.INTERNAL_TOKEN = None

    def req(**headers):
        return SimpleNamespace(headers=CIMultiDict(headers), remote="10.9.9.9")

    check("x-api-key -> Client", auth.resolve_client(req(**{"x-api-key": tok})) == ("agent-cli", "x-api-key"))
    check("Bearer -> Client", auth.resolve_client(req(Authorization=f"Bearer {tok}")) == ("agent-cli", "bearer"))
    check("Bearer geht vor x-api-key", auth.resolve_client(req(Authorization="Bearer falsch", **{"x-api-key": tok}))
          == (None, "bad_token"))
    check("falscher x-api-key -> bad_token, kein IP-Rueckfall", auth.resolve_client(req(**{"x-api-key": "falsch"})) == (None, "bad_token"))
    check("nichts geschickt -> (None, None)", auth.resolve_client(req()) == (None, None))


def t_modelle():
    tags = [{"name": "code:latest", "modified_at": "2026-09-30T08:21:00.123Z"}, {"name": "standard:latest"}]
    r = A.models_response(tags)
    b = body_of(r)
    check("Modelle in Anthropic-Form", b["has_more"] is False and b["data"][0] == {
        "type": "model", "id": "code:latest", "display_name": "code:latest", "created_at": "2026-09-30T08:21:00Z"}, b)
    check("Einzelmodell ohne :latest gefunden", body_of(A.model_response(tags, "code"))["id"] == "code:latest")
    check("unbekanntes Modell -> not_found_error", A.model_response(tags, "nix").status == 404)
    check("Anthropic-Clients am Header erkannt", A.wants_anthropic(SimpleNamespace(headers=CIMultiDict({"anthropic-version": "2023-06-01"})))
          and not A.wants_anthropic(SimpleNamespace(headers=CIMultiDict())))


if __name__ == "__main__":
    for t in (t_anfrage, t_verlauf, t_antwort, t_stream, t_fehler, t_auth, t_modelle):
        t()
    print(f"\n{'OK' if not FAILS else 'FEHLER'}: {len(FAILS)} fehlgeschlagen")
    sys.exit(1 if FAILS else 0)
