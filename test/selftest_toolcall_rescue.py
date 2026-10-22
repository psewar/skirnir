#!/usr/bin/env python3
"""Self-test for tool call rescue. Runs without router and network.

The positive cases are VERBATIM the outputs where qwen3-coder:30b failed on
2026-09-15 (measurement series local-toolcall-context) - not a
reconstructed ideal. The negative cases are more important than the positive ones:
a rescue that saves too much causes the client to execute a tool.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "router"))
from skirnir_router import toolcall_rescue as tr   # noqa: E402

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


TOOLS = [
    {"type": "function", "function": {"name": "read_note", "parameters": {"type": "object", "properties": {
        "note_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "set_light", "parameters": {"type": "object", "properties": {
        "entity": {"type": "string"}, "state": {"type": "string"}, "helligkeit": {"type": "integer"}}}}},
]

# --- this is exactly how it came back from Ollama ---
REAL_XML = ("<function=read_note>\n<parameter=note_id>\nZ7QX-4417-VULPES\n</parameter>\n"
            "</function>\n</tool_call>")
REAL_XML_ONE_LINE = "<function=read_note> <parameter=note_id> Z7QX-4417-VULPES </parameter> </function> </tool_call>"


def t_positive():
    for label, txt in [("XML mehrzeilig", REAL_XML), ("XML einzeilig", REAL_XML_ONE_LINE)]:
        msg = {"role": "assistant", "content": txt}
        name = tr.rescue_message(msg, TOOLS)
        check("rettet %s" % label, name == "read_note", str(name))
        check("Argument %s" % label,
              msg.get("tool_calls", [{}])[0].get("function", {}).get("arguments") == {"note_id": "Z7QX-4417-VULPES"},
              str(msg.get("tool_calls")))
        check("content geleert %s" % label, msg.get("content") == "", repr(msg.get("content"))[:40])

    msg = {"role": "assistant", "content": '{"name": "read_note", "arguments": {"note_id": "AB-1"}}'}
    check("rettet JSON-Dialekt", tr.rescue_message(msg, TOOLS) == "read_note", str(msg.get("tool_calls")))

    msg = {"role": "assistant", "content": '<tool_call>{"name":"read_note","arguments":{"note_id":"AB-2"}}</tool_call>'}
    check("rescues JSON in the tool_call envelope", tr.rescue_message(msg, TOOLS) == "read_note", str(msg.get("tool_calls")))

    # Typing: XML yields text, schema says integer
    msg = {"role": "assistant", "content": "<function=set_light><parameter=entity>licht.kueche</parameter>"
                                           "<parameter=state>on</parameter><parameter=helligkeit>80</parameter></function>"}
    tr.rescue_message(msg, TOOLS)
    args = msg.get("tool_calls", [{}])[0].get("function", {}).get("arguments", {})
    check("typisiert integer", args.get("helligkeit") == 80, repr(args.get("helligkeit")))


def t_negative():
    """Nothing should happen here."""
    msg = {"role": "assistant", "content": "Die Notiz Z7QX-4417-VULPES habe ich nicht gefunden."}
    check("leaves prose alone", tr.rescue_message(msg, TOOLS) is None and "tool_calls" not in msg)

    # Tool not offered by the client -> hallucinated, do not rescue
    msg = {"role": "assistant", "content": "<function=run_shell><parameter=command>rm -rf /</parameter></function>"}
    check("rettet KEIN unangebotenes Werkzeug", tr.rescue_message(msg, TOOLS) is None, str(msg.get("tool_calls")))

    # Explanatory text with incidental mention -> no call intended
    lang = ("Ich koennte hier read_note verwenden. Ein Aufruf saehe etwa so aus: "
            "<function=read_note><parameter=note_id>X</parameter></function> "
            "Aber dafuer brauche ich erst die Kennung, und die hast du mir nicht genannt. "
            "Nenne mir bitte die Kennung, dann hole ich die Notiz.")
    msg = {"role": "assistant", "content": lang}
    check("does not rescue from explanation text", tr.rescue_message(msg, TOOLS) is None, str(msg.get("tool_calls")))

    # Already successful call -> do not touch
    msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_note", "arguments": {}}}]}
    check("does not touch real tool_call", tr.rescue_message(msg, TOOLS) is None)

    check("inactive without tools", tr.rescue_message({"content": REAL_XML}, []) is None)
    check("active_for: no without tools", tr.active_for({"messages": []}) is False)
    check("active_for: no with format", tr.active_for({"tools": TOOLS, "format": {"type": "object"}}) is False)
    check("active_for: yes with tools", tr.active_for({"tools": TOOLS}) is True)


def _stream(chunks, tools=TOOLS, done_extra=None):
    """Plays upstream chunks through the rescuer and returns the emitted ones."""
    r = tr.StreamRescuer(tools)
    out_chunks = []
    for s in chunks:
        out_chunks += r.chunk({"model": "m", "message": {"role": "assistant", "content": s}, "done": False})
    last = {"model": "m", "message": {"role": "assistant", "content": ""}, "done": True,
              "done_reason": "stop", "eval_count": 12, "prompt_eval_count": 40}
    last.update(done_extra or {})
    out_chunks += r.chunk(last)
    return r, out_chunks


def t_stream():
    # Call arrives in pieces -> none of it may land as text on the client
    r, out_chunks = _stream(["<function=read_note>", "<parameter=note_id>", "Z7QX-4417-VULPES",
                       "</parameter>", "</function>"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in out_chunks)
    check("Stream: no prose leak", text == "", repr(text)[:80])
    tcs = [c for c in out_chunks if ((c.get("message") or {}).get("tool_calls"))]
    check("Stream: Aufruf gerettet", len(tcs) == 1, str(out_chunks)[:120])
    if tcs:
        fn = tcs[0]["message"]["tool_calls"][0]["function"]
        check("Stream: richtiger Aufruf",
              fn["name"] == "read_note" and fn["arguments"] == {"note_id": "Z7QX-4417-VULPES"}, str(fn))
    check("Stream: counters remain at the end",
          any(c.get("done") and c.get("eval_count") == 12 for c in out_chunks), str(out_chunks)[-140:])

    # Normal prose -> pass through completely and in order
    r, out_chunks = _stream(["Hal", "lo ", "Welt"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in out_chunks)
    check("Stream: prose unchanged", text == "Hallo Welt", repr(text))
    check("Stream: exactly one completion", sum(1 for c in out_chunks if c.get("done")) == 1, str(len(out_chunks)))

    # Prose that starts like a call but isn't one
    r, out_chunks = _stream(["{", "\"a\": 1} ", "war nur JSON im Text"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in out_chunks)
    check("Stream: failed start is retried", text == '{"a": 1} war nur JSON im Text', repr(text))

    # Ollama handles the call itself -> pass through, swallow nothing
    r = tr.StreamRescuer(TOOLS)
    out_chunks = r.chunk({"model": "m", "done": False, "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": "read_note", "arguments": {"note_id": "Q"}}}]}})
    check("Stream: real tool_call passes through",
          len(out_chunks) == 1 and (out_chunks[0].get("message") or {}).get("tool_calls"), str(out_chunks)[:100])

    # do not intervene at all without tools
    r, out_chunks = _stream(["<function=read_note>", "</function>"], tools=[])
    text = "".join((c.get("message") or {}).get("content") or "" for c in out_chunks)
    check("Stream: unchanged without tools", text == "<function=read_note></function>", repr(text)[:60])


def t_lost():
    """The case the tier does NOT fix - it should at least be noticeable."""
    foreign_call = "<function=run_shell><parameter=command>ls</parameter></function>"
    check("suspicion: foreign tool stands out", tr.suspicious(foreign_call) is True)
    check("suspicion: not prose", tr.suspicious("Die Notiz gibt es leider nicht.") is False)
    check("suspicion: not empty", tr.suspicious("") is False)
    # Interaction: not rescued AND suspicious = the reportable case
    msg = {"role": "assistant", "content": foreign_call}
    check("not rescued, but reported",
          tr.rescue_message(msg, TOOLS) is None and tr.suspicious(msg["content"]) is True)

    # Stream: unknown dialect -> text goes out (do not swallow!) and is reported
    r, out_chunks = _stream(["<function=run_shell>", "<parameter=command>ls</parameter>", "</function>"])
    text = "".join((c.get("message") or {}).get("content") or "" for c in out_chunks)
    check("Stream: unrescuable call is NOT swallowed", text == foreign_call, repr(text)[:80])
    check("Stream: unrescuable call is reported", r.lost is True and r.rescued is None)


if __name__ == "__main__":
    t_positive()
    t_negative()
    t_stream()
    t_lost()
    print()
    if FAILS:
        print("FEHLGESCHLAGEN: " + ", ".join(FAILS))
        sys.exit(1)
    print("all checks passed")
